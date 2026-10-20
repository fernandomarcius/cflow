"""Resolution of DERIVED parameters at execution time (backend).

A parameter may declare
`derivacao = { origem?, de, campo_id, campo_valor, substituicoes?, caixa_alta?, juntar?, ... }`:
its value is COMPUTED from another parameter (`de`) by reading a MAP: it filters the rows where
`campo_id == value(de)` and takes the DISTINCT values of `campo_valor` (applying substitutions /
UPPER CASE / join). The map ORIGIN varies:
  - `arquivo` (default, backwards compatible): reads a whole `arquivo` connection (`mapa` = the
    connection name);
  - `fonte`: queries any connection (`conexao_id` + optional `sql`), binding the parameters;
  - `no`: uses the OUTPUT of a node of the pipeline ITSELF (`no_id`) as the map; runs the
    sub-DAG of the node's ancestors (source/sql/guided/comparison) in an ephemeral DuckDB,
    without a snapshot.
This used to happen only in the Portal (client); when running from the Studio the parameter was
missing and the `:name` leaked literally to the source. Resolving here makes execution work from
ANY surface (Studio and Portal), with the SAME logic as the Portal.
"""

from __future__ import annotations

import uuid

from ..conexoes import service as conexoes
from ..conexoes.parametros import neutralizar_predicados_vazios
from ..db import transacao
from ..errors import ErroAplicacao
from ..execucao.snapshots import resolver_snapshot
from .comparacao import gerar_sql_comparacao
from .definicao import ordem_topologica, parametros_declarados, resolver_derivados_competencia
from .sql_guiado import gerar_sql, validar_uniao


def _campo(linha: dict, nome: str):
    """Read a field of the map row, tolerating case/whitespace variations in the column name."""
    if nome in linha:
        return linha[nome]
    alvo = nome.lower().strip()
    for k, v in linha.items():
        if str(k).lower().strip() == alvo:
            return v
    return None


def _aplicar_subs(d: dict, v: str) -> str:
    for s in d.get("substituicoes") or []:
        if str(s.get("de", "")).strip().lower() == v.strip().lower():
            return s.get("para", v)
    return v.upper() if d.get("caixa_alta") else v


def _resolver_um(d: dict, linhas: list[dict], filtro: str) -> str:
    """Distinct `campo_valor` values in the rows with `campo_id == filtro`.

    Applies join / substitutions / upper case according to the derivation.
    """
    campo_id = d.get("campo_id")
    campo_valor = d.get("campo_valor")
    vistas: list[str] = []
    visto: set[str] = set()
    for linha in linhas:
        if str(_campo(linha, campo_id) or "").strip() != filtro:
            continue
        v = str(_campo(linha, campo_valor) or "").strip()
        if v and v not in visto:
            visto.add(v)
            vistas.append(v)
    juntar = d.get("juntar")
    if juntar is not None:
        return str(juntar).join(vistas)
    return _aplicar_subs(d, vistas[0] if vistas else "")


def _chave_cache(d: dict) -> str:
    """Stable cache key for a map's rows, per origin (the same origin is read once)."""
    origem = d.get("origem", "arquivo")
    if origem == "fonte":
        return f"fonte:{d.get('conexao_id')}:{d.get('sql') or ''}"
    if origem == "no":
        return f"no:{d.get('no_id')}"
    return f"arquivo:{d.get('mapa')}"


# Node types accepted as a MAP (origin `no`): pure transformations and reads. Effects (escrita_sql/
# http) and code (python) are OUT: a map cannot have side effects or a high cost.
_TIPOS_NO_MAPA = ("fonte", "sql", "guiada", "comparacao", "saida")


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


async def _materializar_no_mapa(definicao: dict, no_id: str, valores: dict) -> list[dict]:
    """Materialize the output of node `no_id` of the pipeline ITSELF as map rows.

    Runs the sub-DAG of the node's ancestors in an ephemeral DuckDB (no snapshot, no cache):
    sources are read IN FULL (the map is usually small/filtered), transformations use the SAME
    SQL as execution. Only accepts transformation/read nodes (`_TIPOS_NO_MAPA`); effect/code
    nodes raise an error.

    It does not take `conn`: the sources here are read IN FULL, and holding the metadata
    transaction meanwhile would block autovacuum for the whole database. The two metadata
    queries that exist (origin snapshot, connection list) open their own short transaction.
    """
    import duckdb

    nos = {n["id"]: n for n in definicao.get("nos", [])}
    if no_id not in nos:
        raise ErroAplicacao(
            codigo="derivacao_no_inexistente",
            mensagem=f"Derivation: map node '{no_id}' does not exist in the pipeline.",
            status=400,
        )
    necessarios = _ancestrais(definicao, no_id)
    ordem = [nid for nid in ordem_topologica(definicao) if nid in necessarios]
    vazios = {k for k, v in valores.items() if v in (None, "")}
    duck = duckdb.connect()
    try:
        for nid in ordem:
            no = nos[nid]
            tipo = no.get("tipo")
            if tipo not in _TIPOS_NO_MAPA:
                raise ErroAplicacao(
                    codigo="derivacao_no_invalido",
                    mensagem=(
                        f"Derivation: map node '{no_id}' depends on '{nid}' (type '{tipo}'), "
                        "which is not allowed as a map (use source/sql/guided/comparison)."
                    ),
                    status=400,
                )
            preds = [a["de"] for a in definicao.get("arestas", []) if a["para"] == nid]
            if tipo == "fonte":
                cfg_no = no["config"]
                if (cfg_no.get("origem") or "conexao") == "pipeline":
                    async with transacao() as conn:  # snapshot metadata only; closes right after
                        origem = await resolver_snapshot(
                            conn,
                            cfg_no["pipeline_id"],
                            cfg_no["no_id"],
                            versao_numero=cfg_no.get("versao"),
                        )
                    alias = f"fonte_{nid}"
                    snap_origem = origem["snapshot"]
                    duck.execute(
                        f"CREATE TABLE \"{alias}\" AS SELECT * FROM read_parquet('{snap_origem}')"
                    )
                    sql_p = cfg_no.get("sql")
                    base = sql_p if sql_p and sql_p.strip() else f'SELECT * FROM "{alias}"'
                    duck.execute(f'CREATE TABLE "{nid}" AS {base}')
                else:
                    cid = cfg_no.get("conexao_id")
                    if not cid:
                        raise ErroAplicacao(
                            codigo="fonte_sem_conexao",
                            mensagem=f"Derivation: source node '{nid}' has no connection.",
                            status=400,
                        )
                    sql_fonte = neutralizar_predicados_vazios(cfg_no["sql"], vazios)
                    cols, linhas = await conexoes.amostra(
                        uuid.UUID(str(cid)), sql_fonte, None, valores
                    )
                    _carregar(duck, nid, linhas, cols)
            elif tipo == "sql":
                duck.execute(f'CREATE TABLE "{nid}" AS {no["sql"]}')
            elif tipo == "guiada":
                op_g = no["config"]["op"]
                validar_uniao(duck, op_g, preds)
                duck.execute(f'CREATE TABLE "{nid}" AS {gerar_sql(op_g, entradas=preds)}')
            elif tipo == "comparacao":
                cfg_c = no.get("config") or {}
                esq = cfg_c.get("esquerda") or preds[0]
                dir_ = cfg_c.get("direita") or preds[1]
                duck.execute(
                    f'CREATE TABLE "{nid}" AS '
                    + gerar_sql_comparacao(
                        esq,
                        dir_,
                        cfg_c.get("chave"),
                        incluir_iguais=bool(cfg_c.get("incluir_iguais")),
                    )
                )
            elif tipo == "saida":
                if not preds:
                    raise ErroAplicacao(
                        codigo="saida_sem_entrada",
                        mensagem=f"Derivation: output node '{nid}' has no input.",
                        status=400,
                    )
                duck.execute(f'CREATE TABLE "{nid}" AS SELECT * FROM "{preds[0]}"')
        cur = duck.execute(f'SELECT * FROM "{no_id}"')
        colunas = [c[0] for c in cur.description]
        return [dict(zip(colunas, row, strict=True)) for row in cur.fetchall()]
    finally:
        duck.close()


def _carregar(duck, nome: str, linhas: list[dict], colunas: list[str] | None) -> None:
    """Create table `nome` in duck from rows (dicts). Explicit columns guarantee the schema even
    with 0 rows (an empty map does not break the sub-DAG)."""
    cols = colunas or (list(linhas[0].keys()) if linhas else [])
    if not cols:
        duck.execute(f'CREATE TABLE "{nome}" (vazio INTEGER)')
        return
    defs = ", ".join(f'"{c}" VARCHAR' for c in cols)
    duck.execute(f'CREATE TABLE "{nome}" ({defs})')
    if linhas:
        marcadores = ", ".join(["?"] * len(cols))
        duck.executemany(
            f'INSERT INTO "{nome}" VALUES ({marcadores})',
            [[linha.get(c) for c in cols] for linha in linhas],
        )


async def _linhas_do_mapa(
    d: dict, definicao: dict | None, valores: dict, cache: dict[str, list[dict]]
) -> list[dict]:
    """Return the map rows of a derivation `d`, according to `origem`. Caches per origin.
    Read failures become an empty list (the derivation stays absent, same contract as before)."""
    ck = _chave_cache(d)
    if ck in cache:
        return cache[ck]
    origem = d.get("origem", "arquivo")
    linhas: list[dict] = []
    try:
        if origem == "no":
            if definicao is None:
                raise ValueError("origin 'no' requires the pipeline definition")
            linhas = await _materializar_no_mapa(definicao, str(d.get("no_id")), valores)
        elif origem == "fonte":
            cid = d.get("conexao_id")
            sql = d.get("sql") or ""
            vazios = {k for k, v in valores.items() if v in (None, "")}
            sql_eff = neutralizar_predicados_vazios(sql, vazios) if sql.strip() else ""
            _cols, linhas = await conexoes.amostra(uuid.UUID(str(cid)), sql_eff, 5000, valores)
        else:  # arquivo (default)
            mapa = d.get("mapa")
            async with transacao() as conn:  # connection list only (metadata); closes right after
                conns = await conexoes.listar(conn)
            cid = {c["nome"]: c["id"] for c in conns}.get(mapa)
            if not cid:
                raise ValueError("map not found")
            _cols, linhas = await conexoes.amostra(uuid.UUID(str(cid)), "", 5000, None)
    except ErroAplicacao:
        raise  # structural error of the `no` map must surface (invalid format); do not silence
    except Exception:  # noqa: BLE001 - unreadable map: the derivation stays absent
        linhas = []
    cache[ck] = linhas
    return linhas


async def resolver_derivados(
    definicao: dict, valores: dict | None, *, estrito: bool = True
) -> dict:
    """Return `valores` plus the resolved derived parameters. Respects what already came filled in
    (e.g. the Portal already resolved it). Map read failures are silent (the parameter stays
    absent and validation/execution errors follow the normal flow).

    A CALENDAR derivation (`origem: "competencia"`) does not read a map: it is the pure
    computation of `definicao.resolver_derivados_competencia`, done before the maps, and there
    the supplied value is CHECKED, not respected. `estrito=False` is the Studio preview, where
    the preview value wins."""
    valores = resolver_derivados_competencia(definicao, valores, estrito=estrito)
    derivados = [
        p
        for p in parametros_declarados(definicao)
        if isinstance(p, dict)
        and isinstance(p.get("derivacao"), dict)
        and p["derivacao"].get("origem") != "competencia"
        and isinstance(p.get("nome"), str)
    ]
    if not derivados:
        return valores

    cache: dict[str, list[dict]] = {}

    for p in derivados:
        nome = p["nome"]
        if valores.get(nome) not in (None, ""):
            continue  # already resolved by the caller (Portal): respect it
        d = p["derivacao"]
        de = d.get("de")
        if not (de and d.get("campo_id") and d.get("campo_valor")):
            continue
        filtro = str(valores.get(de) or "").strip()
        if not filtro:
            continue
        linhas = await _linhas_do_mapa(d, definicao, valores, cache)
        valor = _resolver_um(d, linhas, filtro)
        if valor != "":
            valores[nome] = valor
    return valores


async def resolver_derivacoes_pagina(execucao: dict | None, valores: dict | None) -> dict:
    """Resolve the derivations declared at PAGE level (`execucao.derivacoes`): same logic as the
    Portal, but in the backend (needed for page SCHEDULING, which fires without the client). The
    filter is the value of the first chosen LOOKUP filter (`tipo: 'lookup'`, or the legacy alias
    `'operacao'`, in `execucao.operador`). Respects what already came resolved (the Portal
    already sends it). Map read failures are silent."""
    valores = dict(valores or {})
    if not isinstance(execucao, dict):
        return valores
    derivacoes = execucao.get("derivacoes") or []
    op_param = next(
        (
            o.get("param")
            for o in (execucao.get("operador") or [])
            if isinstance(o, dict) and o.get("tipo") in ("operacao", "lookup")
        ),
        None,
    )
    filtro = str(valores.get(op_param) or "").strip() if op_param else ""
    if not derivacoes or not filtro:
        return valores

    cache: dict[str, list[dict]] = {}
    for d in derivacoes:
        if not isinstance(d, dict):
            continue
        nome = d.get("param")
        if not nome or valores.get(nome) not in (None, ""):
            continue
        if not (d.get("campo_id") and d.get("campo_valor")):
            continue
        # A PAGE derivation does not have the pipeline graph at hand: origin `no` does not apply here.
        if d.get("origem") == "no":
            continue
        linhas = await _linhas_do_mapa(d, None, valores, cache)
        valor = _resolver_um(d, linhas, filtro)
        if valor != "":
            valores[nome] = valor
    return valores


def _ordenar_opcoes(o: dict):
    """Sort dropdown options: numeric value -> NUMERIC order (1,2,...,10), otherwise alphabetical
    by label. SAME rule as /conexoes/{id}/opcoes (connection-source vs node-source parity)."""
    try:
        return (0, float(o["valor"]), "")
    except (TypeError, ValueError):
        return (1, 0.0, o["rotulo"].lower())


async def opcoes_de_no(
    definicao: dict,
    no_id: str,
    coluna_valor: str,
    coluna_rotulo: str | None = None,
    parametros: dict | None = None,
) -> list[dict]:
    """Dropdown options from the OUTPUT of a pipeline node: materializes the node (ephemeral
    sub-DAG, `_materializar_no_mapa`) and projects DISTINCT `[{valor, rotulo}]`, sorted. Same
    contract as /conexoes/{id}/opcoes; the only difference is the origin (node vs connection)."""
    linhas = await _materializar_no_mapa(definicao, no_id, dict(parametros or {}))
    rotcol = coluna_rotulo or coluna_valor
    vistos: set[str] = set()
    out: list[dict] = []
    for linha in linhas:
        valor = str(_campo(linha, coluna_valor) or "").strip()
        if not valor or valor in vistos:
            continue
        vistos.add(valor)
        rotulo = str(_campo(linha, rotcol) or "").strip() or valor
        out.append({"valor": valor, "rotulo": rotulo})
    out.sort(key=_ordenar_opcoes)
    return out
