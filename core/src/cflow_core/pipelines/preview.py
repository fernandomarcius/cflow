"""Per-node preview with a PERSISTENT SESSION and a cache of the sources' samples.

Each session (user+pipeline) keeps an in-memory DuckDB that lives across previews:
- the sample of each SOURCE is loaded once (key = hash of connection+query+sample) and reused;
  the source is only re-queried if that source's config changes.
- DERIVED nodes (sql/guided) are recreated on every preview (in-memory, cheap) and reflect edits.
This gives immediate feedback (Power Query/Jupyter style) without hitting the source on every click.

A per-session lock serializes concurrent previews of the same session. Sessions expire on
inactivity (TTL) and there is a cap (LRU); on eviction the DuckDB is closed. The session is only a
CACHE: a miss just reloads, so correctness does not depend on process affinity.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..biblioteca_funcoes import aplicar as bf_aplicar
from ..conexoes import service as conexoes
from ..conexoes.diagnostico import causa_consulta
from ..conexoes.parametros import neutralizar_predicados_vazios, preparar_duckdb
from ..config import get_settings
from ..duck import conectar as conectar_duck
from ..errors import DetalheErro, ErroAplicacao
from ..execucao.snapshots import resolver_snapshot
from .alias import aliases_de_rotulos, criar_alias, remover_alias
from .comparacao import gerar_sql_comparacao
from .definicao import (
    _competencia_janela,
    bibliotecas_usadas,
    nomes_segredo,
    ordem_topologica,
)
from .sql_guiado import gerar_sql, validar_uniao

PREVIEW_LIMIT = 200


def _ancestrais(definicao: dict, alvo: str) -> set[str]:
    entrada: dict[str, list[str]] = {}
    for a in definicao.get("arestas", []):
        entrada.setdefault(a["para"], []).append(a["de"])
    vistos: set[str] = set()
    pilha = [alvo]
    while pilha:
        n = pilha.pop()
        if n in vistos:
            continue
        vistos.add(n)
        pilha.extend(entrada.get(n, []))
    return vistos


def _hash_fonte(config: dict, parametros: dict | None = None) -> str:
    """Identity of a source's sample in the preview session.

    Hashes the WHOLE config, not a hand-picked list of fields: an allowlist would pin the stale
    sample whenever a new origin brought a field nobody remembered to add there, and the author
    would edit the node and still see the old data with no warning."""
    chave = {
        "config": config,
        "parametros": parametros or {},  # changing a preview param re-queries the source
    }
    return hashlib.sha256(json.dumps(chave, sort_keys=True, default=str).encode()).hexdigest()


# "Everything" period window (half-open, covers any date) used when the preview has no value:
# the user wants to see ALL results, like an "all", with no month filter.
_JANELA_TUDO = ("0001-01-01", "9999-12-31")


# Parameter values for the PREVIEW (lenient): supplied value -> preview value -> default.
# None of them -> "everything" (period: full window; scalar: NULL, unrestricted). NEVER raises
# (preview is structural; a month with no data -> 0 rows, not an error).
def _params_preview(definicao: dict, parametros: dict | None) -> dict:
    informado = dict(parametros or {})
    eff: dict = {}
    for p in definicao.get("parametros") or []:
        nome = p.get("nome")
        if not nome:
            continue
        if p.get("tipo") == "competencia":
            # A period EXPANDS into :{name}_inicio / :{name}_fim (half-open window). With no
            # preview value -> FULL window (brings ALL months in the preview), not "previous month".
            bruto = informado.get(nome)
            if bruto in (None, ""):
                bruto = p.get("valor_preview") or p.get("default")
            if bruto in (None, ""):
                eff[f"{nome}_inicio"], eff[f"{nome}_fim"] = _JANELA_TUDO
                continue
            try:
                inicio, fim = _competencia_janela(str(bruto))
            except ValueError:
                inicio, fim = _competencia_janela(
                    "anterior"
                )  # lenient: invalid value -> previous month
            eff[f"{nome}_inicio"] = inicio
            eff[f"{nome}_fim"] = fim
            continue
        if informado.get(nome) not in (None, ""):
            eff[nome] = informado[nome]
        elif p.get("valor_preview") not in (None, ""):
            # `valor_preview` is only for the Studio preview (does not affect the real
            # execution): keeps the preview non-empty without needing an execution default.
            eff[nome] = p["valor_preview"]
        elif p.get("default") not in (None, ""):
            eff[nome] = p["default"]
        else:
            # No preview value -> do NOT restrict: bind NULL (brings everything if the SQL uses
            # `(:p IS NULL OR col = :p)`). A per-type placeholder (0/'1900-01-01'/'') would
            # filter out everything or the wrong rows.
            eff[nome] = None
    return eff


# An error the SERVER raised about the QUERY becomes a visible detail: first line, truncated, with
# the `segredo` parameter value redacted (`conexoes.diagnostico`, allow-list by error class).
# Connection/authentication failures stay generic: the driver message describes the connection.
# Without this, a business-guard `RAISE` in Postgres reached the Studio as "invalid SQL or
# source", sending the author to debug a correct SQL without saying the guard had fired.
def _detalhe_causa(exc: BaseException, segredos: Iterable[str] = ()) -> list[DetalheErro] | None:
    causa = causa_consulta(exc, segredos=segredos)
    return [DetalheErro(campo="sql", mensagem=causa)] if causa else None


@dataclass
class _Sessao:
    duck: object  # persistent DuckDB connection
    fontes: dict[str, str] = field(default_factory=dict)  # node_id -> hash of the loaded config
    aliases: dict[str, str] = field(default_factory=dict)  # node_id -> alias (view) created
    ultimo_acesso: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_SESSOES: dict[str, _Sessao] = {}


def _evictar(agora: float) -> None:
    cfg = get_settings()
    ttl = cfg.preview_sessao_ttl_segundos
    # by inactivity
    for chave in [k for k, s in _SESSOES.items() if agora - s.ultimo_acesso > ttl]:
        _fechar(chave)
    # by cap (LRU)
    if len(_SESSOES) > cfg.preview_sessao_max:
        for chave in sorted(_SESSOES, key=lambda k: _SESSOES[k].ultimo_acesso)[
            : len(_SESSOES) - cfg.preview_sessao_max
        ]:
            _fechar(chave)


def _fechar(chave: str) -> None:
    s = _SESSOES.pop(chave, None)
    if s is not None:
        try:
            s.duck.close()
        except Exception:  # noqa: BLE001
            pass


def _obter_sessao(chave: str) -> _Sessao:
    agora = time.monotonic()
    _evictar(agora)
    s = _SESSOES.get(chave)
    if s is None:
        s = _Sessao(duck=conectar_duck("preview"))
        _SESSOES[chave] = s
    s.ultimo_acesso = agora
    return s


def _carregar_amostra(
    duck, nome: str, linhas: list[dict], colunas: list[str] | None = None
) -> None:
    duck.execute(f'DROP TABLE IF EXISTS "{nome}"')
    if linhas:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(linhas, f, ensure_ascii=False, default=str)
            caminho = f.name
        # All columns as VARCHAR (no inference): IDs with leading zeros break auto-inference
        # (same as the engine). Downstream casts (CAST) what it needs.
        # Dedupe + drop empty/None names (phantom spreadsheet columns): read_json with
        # `columns` rejects duplicate names. Falls back to read_json_auto if something slips.
        cols = colunas or list(linhas[0].keys())
        vistos: set = set()
        cols_ok = []
        for c in cols:
            cn = str(c)
            if cn in ("", "None") or cn in vistos:
                continue
            vistos.add(cn)
            cols_ok.append(cn)
        try:
            spec = ", ".join(f"\"{c}\": 'VARCHAR'" for c in cols_ok)
            duck.execute(
                f"CREATE TABLE \"{nome}\" AS SELECT * FROM read_json(?, columns={{{spec}}}, format='array')",
                [caminho],
            )
        except Exception:  # noqa: BLE001 - robust fallback
            duck.execute(f'DROP TABLE IF EXISTS "{nome}"')
            duck.execute(f'CREATE TABLE "{nome}" AS SELECT * FROM read_json_auto(?)', [caminho])
        finally:
            # `delete=False` above is required (DuckDB reopens the file by path), so the price
            # is deleting it here. Otherwise every preview would leave a sample-sized JSON in
            # /tmp, and the preview session is used dozens of times per edit.
            Path(caminho).unlink(missing_ok=True)
    elif colunas:
        # Empty sample, but known schema: create the REAL columns (VARCHAR) so downstream nodes
        # (and the autocomplete) see the columns even in a month with no data.
        defs = ", ".join(f'"{c}" VARCHAR' for c in colunas)
        duck.execute(f'CREATE TABLE "{nome}" ({defs})')
    else:
        duck.execute(f'CREATE TABLE "{nome}" (vazio INTEGER)')


async def _meta_conexao(conn: AsyncConnection, conexao_id) -> dict:
    """Metadata (type + config) of the connection of an EFFECT node, for the dry-run. Reads only
    the metadata row and NEVER opens a connection to the destination. `{}` when the id is
    invalid/missing: previewing a draft whose connection is not chosen yet must work.

    The three effect nodes need the same SELECT, so it lives here.
    """
    if not conexao_id:
        return {}
    try:
        row = (
            await conn.execute(
                text("SELECT tipo, config FROM conexoes WHERE id = :id"),
                {"id": uuid.UUID(str(conexao_id))},
            )
        ).first()
    except (ValueError, TypeError):
        return {}
    if row is None:
        return {}
    config = row.config if isinstance(row.config, dict) else json.loads(row.config or "{}")
    return {"tipo": row.tipo, "config": config or {}}


async def preview_no(
    conn: AsyncConnection,
    *,
    chave: str,
    definicao: dict,
    no_id: str,
    parametros: dict | None = None,
) -> dict:
    """Preview of node `no_id`, reusing the persistent session `chave` (sample cache).
    Binds the runtime parameters (`:name`) with lenient values (default/placeholder)."""
    nos = {n["id"]: n for n in definicao.get("nos", [])}
    if no_id not in nos:
        raise ErroAplicacao(codigo="no_inexistente", mensagem="Node not found.", status=404)
    necessarios = _ancestrais(definicao, no_id)
    ordem = [nid for nid in ordem_topologica(definicao) if nid in necessarios]
    params = _params_preview(definicao, parametros)
    # DERIVED parameters (e.g. `unit` via a map): same resolution as execution, otherwise the
    # nodes downstream of a source filtered by the derived value (e.g. WHERE :unit = ...) would
    # come out EMPTY in the preview. A map read failure is silent (the derived value stays
    # absent, as in execution). `estrito=False`: here the preview value of the derived parameter
    # WINS over the computation (that is what the tab promises); in execution, a supplied value
    # that differs from the computed one is refused.
    from .derivacao import resolver_derivados

    params = await resolver_derivados(definicao, params, estrito=False)
    # Value of a `segredo` parameter that the server message may echo (Postgres quotes the bound
    # value in a cast error). Redacted before the detail reaches the screen.
    valores_segredo = [
        v for k, v in params.items() if k in nomes_segredo(definicao) and isinstance(v, str)
    ]

    sessao = _obter_sessao(chave)
    async with sessao.lock:
        duck = sessao.duck
        # Load the MACROs of the PUBLISHED libraries referenced by the pipeline before any node
        # runs: same DDL as execution (the same SQL runs in preview and in execution). The
        # session is cached and CREATE OR REPLACE is idempotent: reapplying on every preview
        # picks up newly published versions and avoids a stale MACRO.
        await bf_aplicar.carregar_publicadas(duck, conn, bibliotecas_usadas(definicao))
        aliases = aliases_de_rotulos(definicao)
        efeito_alvo: dict | None = (
            None  # filled in the TARGET effect node (`efeito` key of the return)
        )
        # TARGET lake source: the preview DECLARES that it read a slice (N of M partitions)
        # instead of letting the viewer think the count is for the whole dataset.
        amostra_alvo: dict | None = None
        for nid in ordem:
            no = nos[nid]
            tipo = no.get("tipo")
            try:
                if tipo == "fonte":
                    cfg_no = no["config"]
                    origem_no = cfg_no.get("origem") or "conexao"
                    h = _hash_fonte(cfg_no, params)
                    if origem_no == "lake":
                        # The lake sample identity includes WHICH batches exist: without it, a new
                        # ingestion would not invalidate the cache and the author would keep
                        # looking at stale data without having changed anything in the definition.
                        from ..ingestao import leitura as lake_leitura

                        _, lotes_atuais = await lake_leitura.resolver(cfg_no, parametros=params)
                        h = f"{h}:{lake_leitura.assinatura(lotes_atuais)}"
                    if sessao.fontes.get(nid) != h:  # CACHE MISS: fetch from the source
                        if origem_no == "lake":
                            # Sample of the N MOST RECENT partitions (today's data says more than
                            # old data, and downloading 32 files to show 10 rows is absurd). The
                            # SQL of the node on top is BYTE FOR BYTE the same as in execution.
                            from ..ingestao import leitura as lake_leitura

                            n = int(cfg_no.get("amostra", 10))
                            fatias_n = int(
                                cfg_no.get("amostra_fatias") or get_settings().lake_preview_fatias
                            )
                            partes_lake, resumo_lake = await lake_leitura.partes(
                                cfg_no, parametros=params, limite_fatias=fatias_n
                            )
                            # The preview must show the SAME column that execution will produce;
                            # without the flag, the author would design the SQL without the period.
                            sql_lake, binds = lake_leitura.sql_leitura(
                                partes_lake,
                                limite=n,
                                competencia=resumo_lake.get("fatia_e_competencia", False),
                            )
                            alias = f"fonte_{nid}"
                            duck.execute(f'DROP TABLE IF EXISTS "{alias}"')
                            duck.execute(f'CREATE TABLE "{alias}" AS {sql_lake}', binds)
                            sql_p = cfg_no.get("sql")
                            base = sql_p if sql_p and sql_p.strip() else f'SELECT * FROM "{alias}"'
                            duck.execute(f'DROP TABLE IF EXISTS "{nid}"')
                            duck.execute(f'CREATE TABLE "{nid}" AS {base}')
                            if nid == no_id:
                                amostra_alvo = resumo_lake
                        elif origem_no == "publicado":
                            # Source = published artifact (silver/gold): same resolution as
                            # execution (through the scope pointer), only with a LIMIT. The
                            # preview must show the same column that execution will produce.
                            n = int(cfg_no.get("amostra", 10))
                            _, caminhos = await lake_leitura.resolver_publicado(
                                cfg_no, parametros=params
                            )
                            lista = ", ".join(f"'{c}'" for c in caminhos)
                            alias = f"fonte_{nid}"
                            duck.execute(f'DROP TABLE IF EXISTS "{alias}"')
                            duck.execute(
                                f'CREATE TABLE "{alias}" AS '
                                f"SELECT * FROM read_parquet([{lista}]) LIMIT {n}"
                            )
                            sql_p = cfg_no.get("sql")
                            base = sql_p if sql_p and sql_p.strip() else f'SELECT * FROM "{alias}"'
                            duck.execute(f'DROP TABLE IF EXISTS "{nid}"')
                            duck.execute(f'CREATE TABLE "{nid}" AS {base}')
                        elif origem_no == "objeto":
                            # Source = a file from the object store: the SAME resolution as execution.
                            from ..ingestao import objeto as fonte_objeto

                            n = int(cfg_no.get("amostra", 10))
                            lido = await fonte_objeto.resolver(cfg_no, parametros=params)
                            alias = f"fonte_{nid}"
                            duck.execute(f'DROP TABLE IF EXISTS "{alias}"')
                            duck.execute(
                                f'CREATE TABLE "{alias}" AS SELECT * FROM '
                                f"({fonte_objeto.sql_relacao(lido, cfg_no)}) LIMIT {n}"
                            )
                            sql_p = cfg_no.get("sql")
                            base = sql_p if sql_p and sql_p.strip() else f'SELECT * FROM "{alias}"'
                            duck.execute(f'DROP TABLE IF EXISTS "{nid}"')
                            duck.execute(f'CREATE TABLE "{nid}" AS {base}')
                        elif origem_no == "pipeline":
                            # Source = output of another pipeline: sample of the referenced
                            # snapshot (same resolution as execution; preview == execution,
                            # only the size changes).
                            origem = await resolver_snapshot(
                                conn,
                                cfg_no["pipeline_id"],
                                cfg_no["no_id"],
                                versao_numero=cfg_no.get("versao"),
                            )
                            n = int(cfg_no.get("amostra", 10))
                            # Sample of the upstream output under an alias (`fonte_{nid}`), then
                            # the node's optional SQL on top: the SAME SQL as execution, only the
                            # data changes (sample vs full). Without SQL, passes the sample on.
                            alias = f"fonte_{nid}"
                            duck.execute(f'DROP TABLE IF EXISTS "{alias}"')
                            duck.execute(
                                f'CREATE TABLE "{alias}" AS SELECT * FROM '
                                f"read_parquet('{origem['snapshot']}') LIMIT {n}"
                            )
                            sql_p = cfg_no.get("sql")
                            base = sql_p if sql_p and sql_p.strip() else f'SELECT * FROM "{alias}"'
                            duck.execute(f'DROP TABLE IF EXISTS "{nid}"')
                            duck.execute(f'CREATE TABLE "{nid}" AS {base}')
                        else:
                            if not cfg_no.get("conexao_id"):
                                raise ErroAplicacao(
                                    codigo="fonte_sem_conexao",
                                    mensagem="Source node has no connection defined.",
                                    status=400,
                                )
                            # "blank = everything": neutralize the predicates of empty parameters
                            # (NULL in eff) for ANY SQL, instead of filtering by NULL (which
                            # would match nothing).
                            sql_fonte = neutralizar_predicados_vazios(
                                cfg_no["sql"], {k for k, v in params.items() if v is None}
                            )
                            # `conn` is not passed: the external source is read with the metadata
                            # transaction closed (see `conexoes.obter_acesso`).
                            cols, linhas = await conexoes.amostra(
                                uuid.UUID(str(cfg_no["conexao_id"])),
                                sql_fonte,
                                int(cfg_no.get("amostra", 10)),
                                params,
                            )
                            _carregar_amostra(duck, nid, linhas, cols)
                        sessao.fontes[nid] = h
                    # CACHE HIT: reuse the table already loaded
                elif tipo == "sql":
                    # The SQL node sample (default 10) limits the result BEFORE it becomes a
                    # table, same rationale as the source node: a light preview by default, the
                    # author raises it if needed. Never persisted (Studio drops the value on save).
                    lim = int((no.get("config") or {}).get("amostra", 10))
                    sql_no = (no["sql"] or "").strip().rstrip(";")
                    # `:name` becomes `?` + a native bind (DuckDB does not understand `:name`)
                    # and a predicate against an empty parameter becomes `1=1`: same semantics
                    # as the source.
                    sql_no, valores = preparar_duckdb(sql_no, params)
                    duck.execute(f'DROP TABLE IF EXISTS "{nid}"')
                    q = f'CREATE TABLE "{nid}" AS SELECT * FROM ({sql_no}) AS _sub LIMIT {lim}'
                    duck.execute(q, valores)
                elif tipo == "guiada":
                    # inputs come from the EDGES arriving at the node (1 for filter/group, 2 for a
                    # keyed join, 2 or more for a union)
                    preds = [a["de"] for a in definicao.get("arestas", []) if a["para"] == nid]
                    op_g = no["config"]["op"]
                    validar_uniao(duck, op_g, preds)
                    sqlg = gerar_sql(op_g, entradas=preds)
                    # The guided operation condition is free text, so it may reference `:name`.
                    sqlg, valores_g = preparar_duckdb(sqlg, params)
                    duck.execute(f'DROP TABLE IF EXISTS "{nid}"')
                    try:
                        duck.execute(f'CREATE TABLE "{nid}" AS {sqlg}', valores_g)
                    except Exception as exc:  # noqa: BLE001 - DuckDB error sanitized
                        if op_g.get("modo") == "uniao":
                            raise ErroAplicacao(
                                codigo="uniao_incompativel",
                                mensagem=(
                                    f"Union failed at node '{nid}': the columns have "
                                    "incompatible types across the inputs."
                                ),
                                status=400,
                            ) from exc
                        raise
                elif tipo == "comparacao":
                    preds = [a["de"] for a in definicao.get("arestas", []) if a["para"] == nid]
                    if len(preds) < 2:
                        raise ErroAplicacao(
                            codigo="comparacao_sem_entradas",
                            mensagem="Comparison requires two connected inputs.",
                            status=400,
                        )
                    cfg_c = no.get("config") or {}
                    esq = cfg_c.get("esquerda") or preds[0]
                    dir_ = cfg_c.get("direita") or preds[1]
                    sqlc = gerar_sql_comparacao(
                        esq,
                        dir_,
                        cfg_c.get("chave"),
                        incluir_iguais=bool(cfg_c.get("incluir_iguais")),
                    )
                    duck.execute(f'DROP TABLE IF EXISTS "{nid}"')
                    duck.execute(f'CREATE TABLE "{nid}" AS {sqlc}')
                elif tipo == "python":
                    # Procedural code in a sandbox (preview == execution, only the size/sample
                    # changes). Exports the predecessors (sampled, in memory) to temporary
                    # Parquet, runs the code isolated and reloads the result. Publishes nothing.
                    import tempfile as _tf
                    from pathlib import Path as _P

                    from ..execucao.sandbox import executar_python

                    preds = [a["de"] for a in definicao.get("arestas", []) if a["para"] == nid]
                    cfg_py = no.get("config") or {}
                    codigo = cfg_py.get("codigo")
                    if not codigo or not str(codigo).strip():
                        raise ErroAplicacao(
                            codigo="python_sem_codigo",
                            mensagem="Code node has no code.",
                            status=400,
                        )
                    with _tf.TemporaryDirectory(prefix="cflow_pyprev_") as _td:
                        entradas = {}
                        for p in preds:
                            cam = _P(_td) / f"{p}.parquet"
                            duck.execute(f"COPY \"{p}\" TO '{cam}' (FORMAT parquet)")
                            entradas[p] = str(cam)
                        saida_p = _P(_td) / f"{nid}.parquet"
                        executar_python(str(codigo), entradas, params, saida_p)
                        duck.execute(f'DROP TABLE IF EXISTS "{nid}"')
                        duck.execute(
                            f"CREATE TABLE \"{nid}\" AS SELECT * FROM read_parquet('{saida_p}')"
                        )
                elif tipo == "saida":
                    # Preview NEVER publishes: it only shows what would come out (passes the input on).
                    preds = [a["de"] for a in definicao.get("arestas", []) if a["para"] == nid]
                    if not preds:
                        raise ErroAplicacao(
                            codigo="saida_sem_entrada",
                            mensagem="Output has no connected input node.",
                            status=400,
                        )
                    duck.execute(f'DROP TABLE IF EXISTS "{nid}"')
                    duck.execute(f'CREATE TABLE "{nid}" AS SELECT * FROM "{preds[0]}"')
                elif tipo == "ingestao":
                    # DRY-RUN: shows the window and the slices that WOULD be requested, without
                    # touching the network. This matters more here than for other effect nodes:
                    # each slice is a request to a public origin, and a preview that really
                    # downloaded would get IP-blocked on every keystroke in the editor. The slicing
                    # uses the SAME function as execution, so the displayed count is what will run.
                    from datetime import date as _date

                    from ..execucao import no_ingestao
                    from ..ingestao import repositorio as repo_ing

                    cfg_i = no.get("config") or {}
                    fonte_i = await repo_ing.carregar_fonte(
                        conn, uuid.UUID(no_ingestao.fonte_id_de(cfg_i, nid))
                    )
                    wm = (
                        (
                            await conn.execute(
                                text(
                                    "SELECT chave_lote FROM ingestao_watermarks WHERE fonte_id = :f"
                                ),
                                {"f": fonte_i["id"]},
                            )
                        )
                        .mappings()
                        .first()
                    )
                    plano_i = no_ingestao.plano_dry_run(
                        fonte=fonte_i, watermark=dict(wm) if wm else None, cfg=cfg_i
                    )
                    # The preview table has the SAME columns as the real snapshot (the author sees
                    # the same grid on both screens), with `desfecho` explicitly 'simulado',
                    # never a plausible outcome that did not happen.
                    no_ingestao.criar_tabela_log(duck, nid)
                    for fatia_i in plano_i["fatias"]:
                        no_ingestao.inserir_log(
                            duck,
                            nid,
                            [
                                {
                                    "chave_lote": fatia_i["chave"],
                                    "janela_inicio": _date.fromisoformat(fatia_i["inicio"]),
                                    "janela_fim": _date.fromisoformat(fatia_i["fim"]),
                                    "desfecho": "simulado",
                                }
                            ],
                        )
                    if nid == no_id:
                        efeito_alvo = {
                            "tipo": "ingestao",
                            "dataset": plano_i["dataset"],
                            "classe": plano_i["classe"],
                            "janela": plano_i["janela"],
                            "fatias": len(plano_i["fatias"]),
                            "watermark": plano_i["watermark"],
                            "aviso": (
                                "Dry-run: nothing was downloaded from the origin or written to the lake. "
                                f"Execution would request {len(plano_i['fatias'])} slice(s)."
                            ),
                        }
                elif tipo == "escrita_sql":
                    # DRY-RUN: builds the plan (final SQL) and materializes the SIMULATED summary.
                    # NEVER connects to the target; it only reads the connection TYPE (metadata)
                    # for the dialect.
                    from ..execucao.no_escrita_sql import criar_tabela_resumo, montar_plano

                    preds = [a["de"] for a in definicao.get("arestas", []) if a["para"] == nid]
                    if len(preds) != 1:
                        raise ErroAplicacao(
                            codigo="escrita_sem_entrada",
                            mensagem="Write requires one connected input.",
                            status=400,
                        )
                    cfg_e = no.get("config") or {}
                    colunas_pred = [
                        d[0]
                        for d in duck.execute(f'SELECT * FROM "{preds[0]}" LIMIT 0').description
                    ]
                    # Rows of the input SAMPLE (not of the target: the dry-run never reads the destination).
                    linhas_entrada = int(
                        duck.execute(f'SELECT count(*) FROM "{preds[0]}"').fetchone()[0]
                    )
                    meta_cx = await _meta_conexao(conn, cfg_e.get("conexao_id"))
                    tipo_cx = meta_cx.get("tipo") or "postgres"
                    plano = montar_plano(cfg_e, colunas_pred, tipo_cx)
                    criar_tabela_resumo(duck, nid)
                    duck.execute(
                        f'INSERT INTO "{nid}" VALUES (?, ?, ?, NULL, ?, NULL)',
                        [plano.destino, plano.estrategia or "sql", linhas_entrada, "simulado"],
                    )
                    if nid == no_id:
                        efeito_alvo = {
                            "tipo": "escrita_sql",
                            "modo": plano.modo,
                            "destino": plano.destino,
                            "sql_gerado": plano.sql_gerado,
                            "linhas_entrada": linhas_entrada,
                            "aviso": "Dry-run: no effect was executed.",
                        }
                elif tipo == "http":
                    # DRY-RUN: builds the requests over the SAMPLE and simulates the response from
                    # `resposta_exemplo`. NEVER fires a request; it only reads the connection base
                    # URL (metadata) to build the final URL, EXACTLY as execution builds it.
                    from ..execucao.no_http import (
                        achatar_unico,
                        colunas_snapshot,
                        linha_snapshot,
                        montar_requisicoes,
                        payload_json,
                    )

                    preds = [a["de"] for a in definicao.get("arestas", []) if a["para"] == nid]
                    cfg_h = no.get("config") or {}
                    modo = cfg_h.get("modo", "unico")
                    if modo in ("por_linha", "lote") and len(preds) != 1:
                        raise ErroAplicacao(
                            codigo="http_sem_entrada",
                            mensagem="HTTP (por_linha/lote) requires one connected input.",
                            status=400,
                        )
                    colunas_pred, linhas_pred = [], []
                    if preds:
                        cur_p = duck.execute(f'SELECT * FROM "{preds[0]}"')
                        colunas_pred = [d[0] for d in cur_p.description]
                        linhas_pred = [
                            dict(zip(colunas_pred, row, strict=True)) for row in cur_p.fetchall()
                        ]
                    meta_cx = await _meta_conexao(conn, cfg_h.get("conexao_id"))
                    base_url = meta_cx.get("config", {}).get("url", "")
                    plano = montar_requisicoes(cfg_h, colunas_pred, linhas_pred, params, base_url)
                    # SIMULATED response from `resposta_exemplo` (schema contract for downstream):
                    # the dry-run does not call the API, so `_status/_erro/_resposta` come from the
                    # example (or NULL).
                    exemplo = cfg_h.get("resposta_exemplo")
                    sim = {"status": 200 if exemplo else None, "resposta": exemplo, "erro": None}
                    if modo == "unico":
                        cols, linhas_snap = (
                            achatar_unico(exemplo, 200, plano.resposta_json_path)
                            if exemplo
                            else (["_status"], [])
                        )
                        _carregar_amostra(duck, nid, linhas_snap, cols)
                    else:
                        linhas_snap = [
                            linha_snapshot(
                                modo,
                                req,
                                sim,
                                colunas_pred,
                                plano.resposta_campos,
                                plano.incluir_resposta_completa,
                            )
                            for req in plano.requisicoes
                        ]
                        _carregar_amostra(
                            duck,
                            nid,
                            linhas_snap,
                            colunas_snapshot(
                                modo,
                                colunas_pred,
                                plano.resposta_campos,
                                plano.incluir_resposta_completa,
                            ),
                        )
                    if nid == no_id:
                        # payloads (<=20) and the final URL built HERE are byte-identical to the
                        # ones in execution over the same sample (same `montar_requisicoes`).
                        payloads = [payload_json(req.corpo) for req in plano.requisicoes[:20]]
                        efeito_alvo = {
                            "tipo": "http",
                            "modo": modo,
                            "metodo": plano.metodo,
                            "destino": plano.requisicoes[0].url
                            if plano.requisicoes
                            else plano.url_destino,
                            "payloads": payloads,
                            "contagem_alvo": len(plano.requisicoes),
                            "aviso": "Dry-run: no effect was executed.",
                        }
                elif tipo == "email":
                    # DRY-RUN: builds the COMPLETE message (subject, rendered body, attachments in
                    # bytes) with the SAME `montar_plano` as execution, over the SAMPLE, and
                    # materializes the SIMULATED summary. NEVER opens an SMTP socket; it only reads
                    # the connection metadata (host/sender) to show who would be sending.
                    from ..execucao.no_email import (
                        DadosDuckDB,
                        criar_tabela_resumo,
                        linha_resumo,
                        montar_plano,
                    )

                    preds = [a["de"] for a in definicao.get("arestas", []) if a["para"] == nid]
                    cfg_m = no.get("config") or {}
                    if len(preds) > 1:
                        raise ErroAplicacao(
                            codigo="email_entradas",
                            mensagem="E-mail accepts at most one input.",
                            status=400,
                        )
                    meta_cx = await _meta_conexao(conn, cfg_m.get("conexao_id"))
                    config_cx = meta_cx.get("config", {})
                    plano = montar_plano(
                        cfg_m,
                        dados=DadosDuckDB(duck),
                        entrada=preds[0] if preds else None,
                        params=params,
                        config_conexao=config_cx,
                    )
                    criar_tabela_resumo(duck, nid)
                    for msg in plano.mensagens:
                        duck.execute(
                            f'INSERT INTO "{nid}" VALUES (?, ?, ?, ?, ?, ?, ?)',
                            linha_resumo(msg, status="simulado", duracao_ms=None, erro=None),
                        )
                    if nid == no_id:
                        primeira = plano.mensagens[0] if plano.mensagens else None
                        efeito_alvo = {
                            "tipo": "email",
                            "modo": plano.modo,
                            "servidor": plano.host_destino,
                            "remetente": plano.remetente,
                            "destino": primeira.destinatarios if primeira else "",
                            "assunto": primeira.assunto if primeira else "",
                            "corpo_tipo": "texto"
                            if primeira and not primeira.corpo_html
                            else "html",
                            "corpo": primeira.corpo if primeira else "",
                            "anexos": [
                                {"nome": a.nome, "bytes": a.bytes, "tipo_mime": a.tipo_mime}
                                for a in (primeira.anexos if primeira else ())
                            ],
                            "contagem_alvo": plano.total,
                            "nao_resolvidos": plano.nao_resolvidos,
                            "aviso": "Dry-run: no e-mail was sent.",
                        }
                else:
                    raise ErroAplicacao(
                        codigo="tipo_no",
                        mensagem=f"Node type '{tipo}' is not supported.",
                        status=400,
                    )
            except ErroAplicacao:
                raise
            except Exception as exc:  # noqa: BLE001 - SQL/source error sanitized
                import logging

                logging.getLogger("cflow_core").warning("preview_no failed at %s: %s", nid, exc)
                raise ErroAplicacao(
                    codigo="erro_preview",
                    mensagem=f"Failed to preview node '{nid}': invalid SQL or source.",
                    status=400,
                    detalhes=_detalhe_causa(exc, valores_segredo),
                ) from exc
            # Label as ALIAS (view) of the node table; the id remains valid (see alias.py).
            # If renamed, drop the old view, otherwise the old name would keep resolving only in
            # the preview (diverging from execution).
            alias_novo = aliases.get(nid)
            alias_antigo = sessao.aliases.get(nid)
            if alias_antigo and alias_antigo != alias_novo:
                remover_alias(duck, alias_antigo)
                sessao.aliases.pop(nid, None)
            if alias_novo:
                criar_alias(duck, nid, alias_novo)
                sessao.aliases[nid] = alias_novo

        cur = duck.execute(f'SELECT * FROM "{no_id}" LIMIT {PREVIEW_LIMIT}')
        colunas = [d[0] for d in cur.description]
        linhas = [dict(zip(colunas, row, strict=False)) for row in cur.fetchall()]
        contagem = duck.execute(f'SELECT count(*) FROM "{no_id}"').fetchone()[0]
        sessao.ultimo_acesso = time.monotonic()
        resp = {
            "colunas": colunas,
            "tipos": [str(d[1]) for d in cur.description],
            "linhas": linhas,
            "contagem": contagem,
        }
        if efeito_alvo is not None:
            # Effect node: extra `efeito` key (dry-run); the endpoint passes it on without breaking the contract.
            resp["efeito"] = efeito_alvo
        if amostra_alvo is not None:
            # Lake source: DECLARES the slice read. Without this, the footer count looks like it is
            # for the whole dataset when it is for one slice, and the author designs the SQL with
            # a wrong number.
            resp["amostra_alvo"] = amostra_alvo
        return resp


async def colunas_disponiveis(
    conn: AsyncConnection, *, chave: str, definicao: dict, no_id: str
) -> dict:
    """Columns available to a node = columns of each upstream node (direct predecessors).

    Feeds the SQL editor autocomplete. Reuses the preview session (sample cache), so it is cheap
    after the first computation. Returns {previous_node_id: [columns]}; a predecessor with an
    aliasable label (alias.py) appears TWICE, by id and by label.
    """
    preds = [a["de"] for a in definicao.get("arestas", []) if a["para"] == no_id]
    aliases = aliases_de_rotulos(definicao)
    out: dict[str, list[str]] = {}
    for p in preds:
        try:
            res = await preview_no(conn, chave=chave, definicao=definicao, no_id=p)
            out[p] = res["colunas"]
        except ErroAplicacao:
            out[p] = []
        if p in aliases:
            out[aliases[p]] = out[p]
    return out


def limpar_sessao(chave: str) -> None:
    """Close and discard the preview session (e.g. when leaving the editor)."""
    _fechar(chave)
