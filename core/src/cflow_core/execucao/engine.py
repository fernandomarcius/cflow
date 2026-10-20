from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import date as _date
from datetime import datetime
from datetime import time as _time
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from ..audit import Acao, registrar
from ..biblioteca_funcoes import aplicar as bf_aplicar
from ..biblioteca_funcoes import service as bf_service
from ..conexoes import service as conexoes
from ..conexoes.colunar import Extracao, colunas_validas
from ..conexoes.conectores import get_conector_email, get_conector_escrita
from ..conexoes.parametros import (
    neutralizar_predicados_vazios,
    preparar_duckdb,
    refs_declaradas,
)
from ..config import get_settings
from ..db import autocommit, transacao
from ..errors import DetalheErro, ErroAplicacao
from ..io_lake import em_thread_lake
from ..pipelines import service as ps
from ..pipelines.alias import aliases_de_rotulos, criar_alias
from ..pipelines.comparacao import gerar_sql_comparacao
from ..pipelines.definicao import (
    bibliotecas_usadas,
    completar_declarados,
    filtrar_parametros_declarados,
    normalizar_parametros,
    normalizar_parcial,
    ordem_topologica,
    parametros_declarados,
    parametros_relativos,
    redigir_segredos,
)
from ..pipelines.derivacao import resolver_derivacoes_pagina, resolver_derivados
from ..pipelines.sql_guiado import gerar_sql, validar_uniao
from ..voo_unico import VooUnico
from . import instancia, no_ingestao, reconciliacao
from .bloqueante import em_thread
from .no_email import DadosDuckDB, executar_email
from .no_email import criar_tabela_resumo as criar_tabela_resumo_email
from .no_email import montar_plano as montar_plano_email
from .no_escrita_sql import criar_tabela_resumo, executar_escrita, montar_plano
from .no_http import (
    achatar_unico,
    colunas_snapshot,
    executar_http,
    linha_snapshot,
    montar_requisicoes,
)
from .paralelo import (
    MAX_NOS_EM_PARALELO,
    MAX_NOS_IO_EM_PARALELO,
    dependencias_dos_nos,
    rodar_por_dependencia,
    so_publica,
)
from .sandbox import executar_python
from .snapshots import resolver_snapshot

#: Dedupe + drop empty/None names - the SAME function as the connector's columnar extraction.
_colunas_validas = colunas_validas


#: Types the columnar path can write EXACTLY as the JSON path leaves them in VARCHAR.
#: Measured against DuckDB's `read_json(columns=VARCHAR)`: `str` stays as is, `bool` becomes
#: `true`/`false`, `float` becomes its `repr` (the token `json.dump` writes), and everything else
#: - which `json.dump` only serializes through `default=str` - becomes `str(v)`.
_TIPOS_COLUNARES = frozenset(
    {str, bool, int, float, Decimal, datetime, _date, _time, uuid.UUID, bytes}
)


def _texto_como_json(valores: list) -> list | None:
    """One column's values as the JSON path would leave them in VARCHAR, or `None` when the
    column has something only that path can handle (list, dict, NaN, unknown type) - then the whole
    table falls back to the JSON path, and the result is the usual one by construction."""
    tipos = set(map(type, valores))
    tipos.discard(type(None))
    if tipos <= {str}:
        return valores
    if not tipos <= _TIPOS_COLUNARES:
        return None
    if float in tipos and not all(math.isfinite(v) for v in valores if type(v) is float):
        return None  # NaN/Infinity: JSON writes an invalid token that falls into `read_json_auto`
    saida: list = []
    for v in valores:
        t = type(v)
        if v is None or t is str:
            saida.append(v)
        elif t is bool:
            saida.append("true" if v else "false")
        elif t is float:
            saida.append(repr(v))
        else:
            saida.append(str(v))
    return saida


def _carregar_colunar(duck, nome: str, linhas: list, cols_ok: list[str]) -> bool:
    """Load through the Arrow table, without the intermediate JSON file. Returns `False` (creating
    nothing) when something falls outside what it can reproduce byte for byte - then the JSON
    path applies.

    Why it exists: a connection source used to reach DuckDB by `json.dump`-ing every row into a
    temp file and `read_json`-ing it. On large tables that costs tens of seconds in `json.dump`
    alone and a lot of disk, to produce text that `str()` already gives. The content is the SAME
    - all VARCHAR, same text, same NULL - and the node's content hash proves it on every run."""
    if not cols_ok or any(type(r) is not dict for r in linhas):
        return False
    if not all(isinstance(k, str) for k in linhas[0]):
        return False  # JSON turns a non-text key into text; `r.get` would not find it
    if any(ch in c for c in cols_ok for ch in "\"'\\"):
        return False  # a name that breaks the `read_json` `spec` goes through `read_json_auto`
    if len({c.casefold() for c in cols_ok}) != len(cols_ok):
        return False  # names differing only in case: DuckDB treats them as the same
    import pyarrow as pa

    arrays = []
    for c in cols_ok:
        texto = _texto_como_json([r.get(c) for r in linhas])
        if texto is None:
            return False
        arrays.append(pa.array(texto, type=pa.string()))
    tabela = pa.table(arrays, names=cols_ok)
    temp = f"__carga_{uuid.uuid4().hex}"
    duck.register(temp, tabela)
    try:
        duck.execute(f'CREATE TABLE "{nome}" AS SELECT * FROM "{temp}"')
    finally:
        duck.unregister(temp)
    return True


def _carregar(duck, nome: str, linhas: list[dict], colunas: list[str] | None = None) -> None:
    duck.execute(f'DROP TABLE IF EXISTS "{nome}"')
    if linhas:
        # Read ALL columns as VARCHAR (no type inference): IDs with leading zeros
        # (e.g. "00123456789") break auto-inference (they become numbers/malformed JSON).
        # Downstream casts what it needs. The schema comes from the connector when available.
        cols_ok = _colunas_validas(colunas or list(linhas[0].keys()))
        if _carregar_colunar(duck, nome, linhas, cols_ok):
            return
        _carregar_json(duck, nome, linhas, cols_ok)
    elif colunas:
        # Empty source, but the schema is known: create the table with the REAL columns (VARCHAR)
        # so downstream nodes that reference them work (empty period -> 0/empty, no crash).
        defs = ", ".join(f'"{c}" VARCHAR' for c in colunas)
        duck.execute(f'CREATE TABLE "{nome}" ({defs})')
    else:
        duck.execute(f'CREATE TABLE "{nome}" (vazio INTEGER)')


def _carregar_extracao(duck, nome: str, extracao: Extracao) -> None:
    """Load from a COMPLETE read of the source (`conexoes.extrair`). With `tabela` (the connector's
    columnar extraction, already in the text `_carregar` would produce), DuckDB reads the Arrow
    directly; without it, it is the usual `_carregar` over the rows - including the empty case,
    which depends on the schema."""
    if extracao.tabela is None:
        _carregar(duck, nome, extracao.linhas or [], extracao.colunas)
        return
    duck.execute(f'DROP TABLE IF EXISTS "{nome}"')
    temp = f"__carga_{uuid.uuid4().hex}"
    duck.register(temp, extracao.tabela)
    try:
        duck.execute(f'CREATE TABLE "{nome}" AS SELECT * FROM "{temp}"')
    finally:
        duck.unregister(temp)


def _carregar_json(duck, nome: str, linhas: list[dict], cols_ok: list[str]) -> None:
    """The usual path: JSON in a temp file and `read_json` with every column as VARCHAR. It remains
    the reference for what the load produces - the columnar path is only used when it matches."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(linhas, f, ensure_ascii=False, default=str)
        caminho = f.name
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
        # `delete=False` above is required (DuckDB reopens the file by path, so it cannot vanish
        # at the end of the `with`) - the price is deleting it here. Without this, every node
        # leaves a data-sized JSON file in /tmp, and the host disk pays for it.
        Path(caminho).unlink(missing_ok=True)


def _hash_conteudo(duck, nome: str) -> str:
    """Deterministic hash of the table CONTENT (independent of row order and Parquet bytes).
    Enables reuse: same data -> same hash, even if the file differs."""
    c, s = duck.execute(
        f'SELECT count(*), coalesce(sum(hash(to_json(t))::HUGEINT), 0) FROM "{nome}" t'
    ).fetchone()
    return f"{c}:{s}"


def _colunas_iguais(duck, nome: str, arquivo: str) -> bool:
    """Are the columns of `nome` those of `arquivo`, in the same order? This is what allows passing
    the Parquet along by link instead of rewriting it: a read that adds a column (hive
    partitioning) makes the file DIFFERENT data from what the node computed."""
    a = [c[0] for c in duck.execute(f'DESCRIBE SELECT * FROM "{nome}"').fetchall()]  # noqa: S608
    # `hive_partitioning=false` is the point: `read_parquet` of the SAME path autodetects the
    # directory keys, so comparing a read with a read always ties. What matters is what is INSIDE
    # the file - which is what the consumer sees when reading the snapshot by the execution path.
    b = [
        c[0]
        for c in duck.execute(
            "DESCRIBE SELECT * FROM read_parquet(?, hive_partitioning=false)", [arquivo]
        ).fetchall()
    ]
    return a == b


def _params_referenciados(no: dict, parametros: dict | None) -> dict:
    """Parameters that the node's TEXT references by `:nome` (the node's SQL + `config`, which
    carries the guided operation's condition). Used only by the fingerprint - see `_fingerprint`."""
    if not parametros:
        return {}
    texto = json.dumps(
        {"sql": no.get("sql"), "config": no.get("config")}, sort_keys=True, default=str
    )
    usados = refs_declaradas(texto, parametros.keys())
    return {k: parametros[k] for k in sorted(usados)}


def _fingerprint(
    no: dict, pred_hashes: list[str], bib_pin: list | None = None, parametros: dict | None = None
) -> str:
    """Identity of a derived node's result: SQL/op + content hashes of the inputs + pinned versions
    of the function libraries (a republished macro changes the result of an SQL/guided node that
    calls it, so it must invalidate incremental reuse)."""
    chave = {
        "tipo": no.get("tipo"),
        "sql": no.get("sql"),
        "op": (no.get("config") or {}).get("op"),
        # the whole config goes into the fingerprint: nodes like `comparacao` are parametrized by
        # config.chave (not by `sql`/`op`), so without this two distinct nodes would collide
        # on reuse.
        "config": no.get("config"),
        "preds": sorted(pred_hashes),
        "bibliotecas": sorted((str(b.get("id")), b.get("numero")) for b in (bib_pin or [])),
    }
    # Code nodes (python) and source nodes (connection) read `parametros` directly (not only via
    # the predecessors' content): without this, changing the parameter would reuse a stale
    # snapshot - the source SQL is a TEMPLATE (`:operacao_id` etc.), the resolved value is not in
    # the text.
    if no.get("tipo") in ("python", "fonte"):
        chave["parametros"] = parametros or {}
    else:
        # An sql/guided node that references `:nome` in its own text is a template the same way.
        # Only the REFERENCED parameters go in - a node that uses no parameter keeps its previous
        # fingerprint (reuse preserved) and changing a parameter does not invalidate nodes that
        # do not read it.
        usados = _params_referenciados(no, parametros)
        if usados:
            chave["parametros"] = usados
    return hashlib.sha256(json.dumps(chave, sort_keys=True, default=str).encode()).hexdigest()


def _params_da_extracao(
    no: dict, params_fonte: dict, parametros: dict | None, *, por_referencia: bool
) -> dict:
    """Parameters that enter the IDENTITY of an extraction (fingerprint of the connection source).

    A connector that binds by reference (`:nome` in the SQL becomes a bind - Postgres, SQL Server)
    only depends on the parameters the SQL cites, with the value that reaches it (declared without
    a value = `None`, which neutralizes the predicate). The others - `api_rest` sends EVERY runtime
    parameter as a query string - depend on all of them, even if they don't appear in the text."""
    if por_referencia:
        return _params_referenciados(no, params_fonte)
    return parametros or {}


#: One extraction per (chain, fingerprint) at a time in this process - see `executar_no`.
VOO_EXTRACAO = VooUnico()


#: Candidates for reuse WITHIN the chain: a sibling node with the same fingerprint that already
#: completed. The `LIMIT 5` exists because the snapshot may have been purged between completion
#: and now - the loop skips whatever is no longer on disk.
_SQL_CAND_CADEIA = (
    "SELECT en.snapshot, en.hash, en.linhas FROM execucao_nos en "
    "JOIN execucoes e ON e.id = en.execucao_id "
    "WHERE en.fingerprint = :fp AND en.status = 'concluida' "
    "AND en.snapshot IS NOT NULL AND e.cadeia_id = :cadeia "
    "ORDER BY en.concluido_em DESC LIMIT 5"
)
_SQL_UPSERT_REUSO = (
    "INSERT INTO execucao_nos (execucao_id, no_id, status, linhas, hash, snapshot, duracao_ms, "
    "fingerprint, reaproveitado, concluido_em) "
    "VALUES (:e, :n, 'concluida', :l, :h, :s, 0, :fp, true, now()) "
    "ON CONFLICT (execucao_id, no_id) DO UPDATE SET "
    "status='concluida', linhas=:l, hash=:h, snapshot=:s, duracao_ms=0, "
    "fingerprint=:fp, reaproveitado=true, erro=NULL, concluido_em=now()"
)


async def _reuso_da_cadeia(
    chave_cadeia: tuple[str, str],
    *,
    execucao_id,
    no_id: str,
    snap: Path,
    cadeia_id,
    fp: str,
    marcar,
    evento_leitura: dict | None = None,
    iniciada_por=None,
) -> dict | None:
    """Did a SIBLING node of this chain already produce this content? Then its snapshot will do:
    hardlink and reuse upsert, without touching the source or opening DuckDB.

    Applies to all three ways a source can enter (connection, `publicado` artifact, another
    pipeline's output) because in all of them the input is IMMUTABLE within the chain: the
    extraction is the same query with the same parameters, and the other two read files whose path
    carries `execucao_id=`.

    **With `evento_leitura`, the LINEAGE goes in the SAME transaction as the upsert.** The reused
    node must answer "which publication did this number come from" exactly like the sibling that
    really read - reusing and leaving a hole in the trail would trade performance for
    auditability, and the trail is append-only: what is lost there cannot be recovered. Without
    lineage, a single statement in autocommit, as before."""
    lembrado = _lembrado_da_cadeia(chave_cadeia)
    com_trilha = evento_leitura is not None
    async with transacao() if com_trilha else autocommit() as conn:
        candidatos = (
            [lembrado]
            if lembrado is not None
            else (await conn.execute(text(_SQL_CAND_CADEIA), {"fp": fp, "cadeia": cadeia_id}))
            .mappings()
            .all()
        )
        for c in candidatos:
            if not os.path.isfile(c["snapshot"]):
                continue
            if c["snapshot"] != str(snap):
                await _trazer_snapshot(c["snapshot"], snap)
            await marcar()
            # On the SAME connection as the lookup: one fewer pool checkout.
            await conn.execute(
                text(_SQL_UPSERT_REUSO),
                {
                    "e": execucao_id,
                    "n": no_id,
                    "l": c["linhas"],
                    "h": c["hash"],
                    "s": str(snap),
                    "fp": fp,
                },
            )
            if com_trilha:
                await registrar(
                    conn,
                    acao=Acao.EXECUTAR,
                    recurso_tipo="leitura_lake",
                    recurso_id=execucao_id,
                    ator_id=iniciada_por,
                    antes=None,
                    depois=evento_leitura,
                )
            _lembrar_no(execucao_id, no_id, c["hash"], snap)
            return {
                "no_id": no_id,
                "linhas": c["linhas"],
                "hash": c["hash"],
                "snapshot": str(snap),
                "duracao_ms": 0,
                "reaproveitado": True,
            }
    return None


def _dir(execucao_id) -> Path:
    return Path(get_settings().snapshot_dir) / str(execucao_id)


#: IMMUTABLE fields of an execution - written on INSERT and never updated - read once per
#: process. Before, EVERY node re-read the row in its own transaction, and every lake output
#: re-read the `pipeline_id` in another: hundreds of round trips to the database for the same
#: data. The cap only keeps the dictionary from growing without bound in a long-lived worker.
#: Hash of a node COMPLETED in this process, by (execution, node), with the identity of the
#: snapshot it describes. A derived node used to read each predecessor's hash from the database
#: (one trip through the loop per node); the value is the same one that was just written. The
#: identity (device, inode, mtime) is checked against the file the node will actually READ: if
#: another replica redid the predecessor (Temporal retry) and replaced the snapshot, the identity
#: changes and the hash comes from the database again.
_HASH_NO: dict[tuple[str, str], tuple[str, tuple[int, int, int]]] = {}
#: Source extraction already completed IN THE CHAIN, by (chain, fingerprint): what the chain
#: cache SELECT would return. Same validation by file identity; absent -> the database, which
#: sees the other replicas.
_SNAP_DA_CADEIA: dict[tuple[str, str], dict] = {}
_TETO_MEMO = 50_000


#: Content hash of a FILE already read in this process, by identity (device, inode, size, mtime).
#: Valid only for PURE PASS-THROUGH - the node is `SELECT * FROM read_parquet(<file>)`, with no
#: SQL - and that is where it is correct: two pass-through nodes of the same file build the SAME
#: view over the SAME bytes, so one's `to_json` is the other's.
#:
#: Not to be confused with inheriting the PRODUCER's hash, which was measured and rejected: there
#: the number described its in-memory TABLE, and the Parquet round trip is not type-neutral.
#: Here both sides are the same read of the same inode.
#:
#: The same input files are read many times in one run, at a few microseconds of hash per row.
_HASH_ARQUIVO: dict[tuple[int, int, int, int], str] = {}


def _identidade(caminho) -> tuple[int, int, int, int] | None:
    try:
        st = os.stat(caminho)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _hash_do_arquivo(caminho) -> str | None:
    ident = _identidade(caminho)
    return None if ident is None else _HASH_ARQUIVO.get(ident)


def _lembrar_hash_do_arquivo(caminho, h: str | None) -> None:
    ident = _identidade(caminho)
    if h is None or ident is None:
        return
    if len(_HASH_ARQUIVO) >= _TETO_MEMO:
        _HASH_ARQUIVO.clear()
    _HASH_ARQUIVO[ident] = h


def _assinatura(caminho) -> tuple[int, int, int] | None:
    try:
        st = os.stat(caminho)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns)


def _lembrar_no(execucao_id, no_id: str, h: str | None, snap: Path) -> None:
    ass = _assinatura(snap)
    if h is None or ass is None:
        return
    if len(_HASH_NO) >= _TETO_MEMO:
        _HASH_NO.clear()
    _HASH_NO[(str(execucao_id), no_id)] = (h, ass)


def _hash_lembrado(execucao_id, no_id: str, snap: Path) -> str | None:
    v = _HASH_NO.get((str(execucao_id), no_id))
    if v is None or _assinatura(snap) != v[1]:
        return None
    return v[0]


def _lembrar_da_cadeia(chave: tuple[str, str], snapshot: str, h: str, linhas: int) -> None:
    ass = _assinatura(snapshot)
    if ass is None:
        return
    if len(_SNAP_DA_CADEIA) >= _TETO_MEMO:
        _SNAP_DA_CADEIA.clear()
    _SNAP_DA_CADEIA[chave] = {"snapshot": snapshot, "hash": h, "linhas": linhas, "ass": ass}


def _lembrado_da_cadeia(chave: tuple[str, str]) -> dict | None:
    v = _SNAP_DA_CADEIA.get(chave)
    if v is None or _assinatura(v["snapshot"]) != v["ass"]:
        return None
    return v


_FATOS_EXECUCAO: dict[str, dict] = {}
_FATOS_EM_VOO: dict[str, asyncio.Future] = {}
_TETO_FATOS = 4096


async def _fatos_da_execucao(execucao_id) -> dict | None:
    chave = str(execucao_id)
    fatos = _FATOS_EXECUCAO.get(chave)
    if fatos is not None:
        return fatos
    # SINGLE FLIGHT: the ready nodes of an execution start together, and each one missed the cache
    # and opened ITS OWN transaction for the same row. The `shield` keeps one node's cancellation
    # from cancelling the read the others are waiting for.
    voo = _FATOS_EM_VOO.get(chave)
    if voo is None:
        voo = _FATOS_EM_VOO[chave] = asyncio.ensure_future(_ler_fatos(execucao_id, chave))
        voo.add_done_callback(lambda _f: _FATOS_EM_VOO.pop(chave, None))
    return await asyncio.shield(voo)


async def _ler_fatos(execucao_id, chave: str) -> dict | None:
    async with transacao() as conn:
        linha = (
            (
                await conn.execute(
                    text(
                        "SELECT pipeline_id, bibliotecas_versoes, cadeia_id, iniciada_por "
                        "FROM execucoes WHERE id = :e"
                    ),
                    {"e": execucao_id},
                )
            )
            .mappings()
            .one_or_none()
        )
    if linha is None:
        return None
    if len(_FATOS_EXECUCAO) >= _TETO_FATOS:
        _FATOS_EXECUCAO.clear()
    fatos = _FATOS_EXECUCAO[chave] = dict(linha)
    return fatos


async def _pipeline_da_execucao(execucao_id):
    """`pipeline_id` of the execution: from the facts cache (`executar_no` fills it at the start
    of the node, so publishing always hits) or, outside it, the usual query."""
    fatos = _FATOS_EXECUCAO.get(str(execucao_id))
    if fatos is not None:
        return fatos["pipeline_id"]
    async with transacao() as conn:
        return (
            await conn.execute(
                text("SELECT pipeline_id FROM execucoes WHERE id = :e"), {"e": execucao_id}
            )
        ).scalar_one_or_none()


# How many partitions are uploaded to the lake at the same time. A large fan-out means thousands
# of objects per run: serially that is a run of tens of minutes, almost all of it idle waiting on
# the network. The cap exists because each in-flight partition holds the whole Parquet in memory
# (the local `COPY` + the upload bytes) - without it, a large fan-out would trade network wait for
# a memory peak proportional to the number of scopes, which is how a worker gets knocked over.
_CONCORRENCIA_PARTICAO = 8


def _competencia_expandida(nome: str, parametros: dict, tipos_parametro: dict | None) -> bool:
    """Delegated to `ingestao.leitura`, where the rule lives: the WRITE (here) and the READ (the
    `publicado` source) must name the same scope, and two copies diverge without erroring - they
    did diverge, and the symptom was `lake_dataset_ausente` on every read."""
    from ..ingestao.leitura import competencia_expandida

    return competencia_expandida(nome, parametros, tipos_parametro)


def particao_de_parametro(
    cfg_particao: list[dict],
    parametros: dict | None,
    *,
    tipos_parametro: dict[str, str] | None = None,
) -> dict[str, str]:
    """Segments that apply to the WHOLE run (origin `parametro`), in declared order.

    A `competencia`-type parameter goes in by the START of the window (day 1, ISO): `2026-08`
    becomes `competencia=2026-08-01`, the same form as the `competencia` partition that is
    already published by column - two names for the same month would make the pointer reader find
    none. Looking up the raw name used to break EVERY publication partitioned by competencia."""
    from ..ingestao.leitura import valor_particao

    fora: dict[str, str] = {}
    for item in cfg_particao:
        if item.get("origem") != "parametro":
            continue
        nome = item["nome"]
        chave = item.get("chave") or nome.lower()
        if parametros and nome in parametros:
            valor = parametros[nome]
        elif parametros and _competencia_expandida(nome, parametros, tipos_parametro):
            valor = parametros[f"{nome}_inicio"]
        else:
            # Structural validation already requires the parameter to be declared; getting here
            # without a value means an optional left blank - and "blank = everything" does not
            # translate into a path: it would publish the whole run under an empty `chave=`.
            raise ErroAplicacao(
                codigo="particao_valor_invalido",
                mensagem=(
                    f"Partition '{chave}': parameter '{nome}' has no value in this execution - "
                    "the publication scope would be undefined."
                ),
                status=400,
            )
        fora[chave] = valor_particao(chave, valor)
    return fora


# Private name kept for compatibility. Migrations use the public name so they do not copy this
# rule: writing and porting must produce the same address byte for byte.
_particao_de_parametro = particao_de_parametro


def _carregar_mapa(duck, mapa: str, cols_part: list, combinacoes: list) -> None:
    """Create and load the fan-out map table (raw combination -> scope index). Synchronous,
    so it fits in a single hop to the thread."""
    colunas_mapa = ", ".join(f"c{j} VARCHAR" for j in range(len(cols_part)))
    duck.execute(f'DROP TABLE IF EXISTS "{mapa}"')
    duck.execute(f'CREATE TEMP TABLE "{mapa}" ({colunas_mapa}, i INTEGER)')
    # `permitir_vazio` with column partitioning: zero rows, zero scopes - and DuckDB refuses
    # `executemany` with an empty list. The node used to die BEFORE the index, and the (legitimate,
    # declared) empty run never got to say it produced no scope at all.
    if not combinacoes:
        return
    # Through a registered Arrow table, not `executemany`: DuckDB's Python binding tries
    # `import pandas` for EACH bound value, and a failing import is not cached - it scans the whole
    # `sys.path` every time. With tens of thousands of combinations that is hundreds of thousands
    # of scans, and the node got several times slower when the process had a long `sys.path`.
    import pyarrow as pa

    tabela = pa.table(
        {
            **{
                f"c{j}": pa.array([c[j] for c in combinacoes], pa.string())
                for j in range(len(cols_part))
            },
            "i": pa.array([c[-1] for c in combinacoes], pa.int32()),
        }
    )
    origem = f"{mapa}_arrow"
    duck.register(origem, tabela)
    try:
        duck.execute(f'INSERT INTO "{mapa}" SELECT * FROM "{origem}"')  # noqa: S608
    finally:
        duck.unregister(origem)


def _metadados_da_saida(duck, no_id: str, cfg_particao: list[dict]):
    """`(node columns, partition columns)` in a single hop to the thread. These are two native
    DuckDB calls, and a native call made on the event loop blocks the WHOLE loop - that is how a
    publication whose primitives add up to a few ms ends up costing far more with dozens of nodes
    in flight."""
    colunas = [d[0] for d in duck.execute(f'SELECT * FROM "{no_id}" LIMIT 0').description]  # noqa: S608
    return colunas, _colunas_particao(duck, no_id, cfg_particao)


def _colunas_particao(duck, no_id: str, cfg_particao: list[dict]) -> list[tuple[str, str]]:
    """(key, real column name) of each `coluna`-origin segment, checked against the node's
    result. A missing column fails the node HERE, before any write: publishing anyway would still
    create a single partition, with the wrong name, looking exactly like good data."""
    reais = {d[0] for d in duck.execute(f'SELECT * FROM "{no_id}" LIMIT 0').description}
    por_minuscula = {c.lower(): c for c in reais}
    saida: list[tuple[str, str]] = []
    for item in cfg_particao:
        if item.get("origem") != "coluna":
            continue
        nome = item["nome"]
        chave = item.get("chave") or nome.lower()
        real = nome if nome in reais else por_minuscula.get(nome.lower())
        if real is None:
            raise ErroAplicacao(
                codigo="particao_coluna_ausente",
                mensagem=(
                    f"Partition '{chave}': the node does not return column '{nome}'. "
                    f"Available columns: {', '.join(sorted(reais)) or '(none)'}."
                ),
                status=400,
            )
        saida.append((chave, real))
    return saida


def _escopos_do_fanout(
    duck,
    no_id: str,
    linhas: int,
    base: dict[str, str],
    colunas: list[tuple[str, str]],
    cfg_particao: list[dict],
) -> list[tuple[dict[str, str], int, list[list]]]:
    """One scope per DISTINCT partition value: (partition, rows, raw combinations of the column).

    Resolves and VALIDATES every value BEFORE any write - a value that cannot become a path must
    fail the node with the bucket still untouched, never in the middle of a large fan-out: half
    published is worse than nothing published, because it looks like a complete dataset.

    A scope is the NORMALIZED value, and several raw combinations may fall into the same one (a
    date/time column collapses to the day). They become ONE object: two `dados.parquet` in the
    same `execucao_id=` would be the second write hitting the lake's immutability - an infra error
    for what is just the same day seen twice."""
    from ..ingestao.leitura import valor_particao

    if not colunas:
        return [(base, linhas, [])]
    projecao = ", ".join(f'CAST("{real}" AS VARCHAR)' for _, real in colunas)
    grupos = duck.execute(
        f'SELECT {projecao}, count(*) AS _cflow_n FROM "{no_id}" GROUP BY ALL ORDER BY ALL'
    ).fetchall()
    # The order of the keys in the path is the one DECLARED in the config, not "parameter first":
    # whoever builds the definition chooses the prefix order, and it decides what a glob can scan.
    ordem = [item.get("chave") or item["nome"].lower() for item in cfg_particao]
    escopos: dict[tuple, tuple[dict[str, str], int, list[list]]] = {}
    for linha in grupos:
        crus = list(linha[:-1])
        valores = {
            chave: valor_particao(chave, v) for (chave, _), v in zip(colunas, crus, strict=True)
        }
        completo = {**base, **valores}
        particao = {c: completo[c] for c in ordem}
        chave_escopo = tuple(particao.items())
        # `append` to the scope's list, not `[*combos, crus]`: recopying the list on every
        # combination was O(k^2) - tens of thousands of date/time combinations in one scope crawled.
        atual = escopos.get(chave_escopo)
        if atual is None:
            escopos[chave_escopo] = (particao, int(linha[-1]), [crus])
        else:
            atual[2].append(crus)
            escopos[chave_escopo] = (atual[0], atual[1] + int(linha[-1]), atual[2])
    _recusar_escopos_que_so_diferem_na_caixa(escopos)
    return list(escopos.values())


def _recusar_escopos_que_so_diferem_na_caixa(escopos: dict[tuple, tuple]) -> None:
    """Two scopes of the SAME run that differ only in case are refused - before the 1st write.

    Backend parity: on GCS `unit_id=abc` and `unit_id=ABC` are two objects; on APFS/HFS+ (the
    macOS default, and air-gapped mode runs on disk) they are the same directory. Without this
    refusal, the same definition would publish two scopes in one place and a single one in the
    other - and on the local lake what failed it was the backend's immutability,
    `objeto_ja_existe` ("the layer is immutable"), an INFRA error for a DATA condition, with
    half the fan-out already written.

    A loud refusal rather than normalization: lowercasing everything would merge the two units
    into one scope and publish one's data under the other's label. Mixed-case codes are the
    normal case here (ABC/XYZ/QRS)."""
    vistos: dict[tuple, tuple] = {}
    for chave in escopos:
        dobra = tuple((c, v.casefold()) for c, v in chave)
        anterior = vistos.get(dobra)
        if anterior is not None and anterior != chave:
            a = "/".join(f"{c}={v}" for c, v in anterior)
            b = "/".join(f"{c}={v}" for c, v in chave)
            raise ErroAplicacao(
                codigo="particao_caixa_conflitante",
                mensagem=(
                    f"Scopes '{a}' and '{b}' differ only in CASE. A lake on disk cannot tell "
                    "them apart and one publication would end up describing the other; in a "
                    "bucket they would be two scopes. Standardize the value's case at the source."
                ),
                status=400,
            )
        vistos[dobra] = chave


def _propagar_falha_do_fanout(resultados: list, escopos: list) -> None:
    """Raise if ANY fan-out task failed, saying WHICH scopes failed.

    Called after `gather(..., return_exceptions=True)`, i.e. with all siblings already
    finished - none keeps writing after the node is marked as failed. The message names the
    scopes because "the publication failed" in a large fan-out does not say where to look."""
    falhas = [
        (escopos[i][0], exc) for i, exc in enumerate(resultados) if isinstance(exc, BaseException)
    ]
    if not falhas:
        return
    primeira = falhas[0][1]
    if isinstance(primeira, ErroAplicacao) and len(falhas) == 1:
        # A data refusal (conflicting case, immutability) already has its own message and
        # points at the scope; wrapping it would replace an explanation with a summary.
        raise primeira
    rotulos = ", ".join(
        "/".join(f"{c}={v}" for c, v in part.items()) or "(no partition)" for part, _ in falhas[:5]
    )
    resto = f" (and {len(falhas) - 5} more)" if len(falhas) > 5 else ""
    motivo = primeira.mensagem if isinstance(primeira, ErroAplicacao) else str(primeira)
    raise ErroAplicacao(
        codigo="particao_publicacao_falhou",
        mensagem=(
            f"Publication failed in {len(falhas)} of {len(escopos)} scope(s): {rotulos}{resto}. "
            f"No pointer was moved - the previous published version remains valid. Reason: {motivo}"
        ),
        status=400,
    ) from primeira


def prefixo_imutavel(
    dataset: str,
    cfg_particao: list[dict],
    base: dict[str, str],
    *,
    camada: str = "silver",
    ambiente: str | None = None,
) -> str:
    """Where this run's INDEX is kept forever, next to the data it describes.

    These are the leading PARAMETER segments - constant across the whole run, unlike the column
    ones, which is exactly what the index enumerates. Only the contiguous leading sequence: a
    parameter segment after a column one is not an ancestor of any path, and the index would end
    up in a directory that does not exist in the data."""
    particao: dict[str, str] = {}
    for item in cfg_particao:
        if item.get("origem") != "parametro":
            break
        chave = item.get("chave") or item["nome"].lower()
        particao[chave] = base[chave]
    if camada != "gold":
        return dataset + "".join(f"/{chave}={valor}" for chave, valor in particao.items())
    from ..ingestao import leitura

    ambiente = ambiente or particao.pop(leitura.CHAVE_AMBIENTE, None)
    ambiente = leitura.ambiente_lake({leitura.CHAVE_AMBIENTE: ambiente}, obrigatorio=True)
    return leitura.prefixo_escopo(dataset, particao, ambiente=ambiente)


_prefixo_imutavel = prefixo_imutavel


def _tmp_ao_lado(destino: Path) -> Path:
    """Temp file in the SAME directory as the destination (same filesystem, so `os.replace` is
    atomic)."""
    return destino.with_name(f".{destino.name}.{uuid.uuid4().hex}.tmp")


def _copiar_atomico(origem: str, destino: Path) -> None:
    """`shutil.copy` is not atomic: a reader of `destino` mid-copy sees a truncated Parquet.
    Today the reader is another process on the same host (the API mounts the same volume and
    serves the snapshot sample through the UI); with the snapshot directory on a network volume
    shared between worker replicas, the reader may be another replica. Writes alongside and
    renames."""
    tmp = _tmp_ao_lado(destino)
    try:
        try:
            # A snapshot is IMMUTABLE: every writer writes to a temp file and swaps it with
            # `os.replace` (new inode), never in place. So two paths to the same inode do not
            # affect each other, and reuse stops costing a copy of the Parquet (for large reused
            # snapshots that was seconds per node).
            os.link(origem, tmp)
        except OSError:
            # Other filesystem (EXDEV) or volume without link support (GCS FUSE): copy, as before.
            shutil.copy(origem, tmp)
        os.replace(tmp, destino)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _ligar_atomico(origem: str, destino: Path) -> bool:
    """The fast path of `_copiar_atomico`: link + rename, microseconds of metadata - runs on the
    loop itself, without the hop to the thread. False when the filesystem refuses the link (EXDEV,
    GCS FUSE): then comes the copy, which is real I/O and goes to the thread."""
    tmp = _tmp_ao_lado(destino)
    try:
        os.link(origem, tmp)
    except OSError:
        return False
    try:
        os.replace(tmp, destino)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return True


async def _trazer_snapshot(origem: str, destino: Path) -> None:
    if not _ligar_atomico(origem, destino):
        # Through the I/O pool, not the activities' one: copying a Parquet is disk wait, and
        # tying up a CPU worker slot with it is what `io_lake` exists to avoid.
        await em_thread_lake(_copiar_atomico, origem, destino)


def _gravar_parquet_atomico(duck, no_id: str, destino: Path) -> None:
    """A direct `COPY ... TO destino` leaves the file visible half-written. Same reason as
    `_copiar_atomico`. Synchronous: runs inside the `_snapshot_e_hash` hop."""
    tmp = _tmp_ao_lado(destino)
    try:
        duck.execute(f"COPY \"{no_id}\" TO '{tmp}' (FORMAT parquet)")
        os.replace(tmp, destino)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _snapshot_e_hash(
    duck, no_id: str, snap: Path, repasse: Path | None, devolver=None, pronto: str | None = None
) -> str:
    """Snapshot, CONTENT hash and - outside the output - handing back the slot, in a single hop to
    the thread (it used to be three, each a trip back through the loop's queue). The hash is what
    gives the count: a separate `count(*)` was one more pass over the same data.

    `pronto` is the hash that ANOTHER NODE IN THIS PROCESS already computed **over the same read of
    the same file** (`_HASH_ARQUIVO`, in pure pass-through). Only that.

    **It is not the producer's hash** - inheriting that was tried and rejected: it describes the
    TABLE the producer held in memory, while this one describes the re-read PARQUET, which is not
    the same quantity. `sum()` over BIGINT gives HUGEINT and does not come back from the file as
    HUGEINT; `test_saida_de_repasse_e_o_parquet_do_predecessor` pins that down. The difference
    here is that both sides are the SAME view over the SAME inode."""
    try:
        if repasse is not None:
            _copiar_atomico(str(repasse), snap)
        else:
            _gravar_parquet_atomico(duck, no_id, snap)
        return pronto or _hash_conteudo(duck, no_id)
    finally:
        if devolver is not None:
            devolver.fechar()


async def _publicar_saida(
    duck,
    no: dict,
    no_id: str,
    linhas: int,
    *,
    execucao_id=None,
    parametros: dict | None = None,
    tipos_parametro: dict[str, str] | None = None,
    sem_publicar: bool = False,
    liberar_duck=None,
) -> str | None:
    """Publish the node's result to a stable, named destination (external consumption), atomically.

    `liberar_duck` (coroutine, optional): hands back the node's DuckDB instance. Called as soon as
    publishing no longer needs it - before the network - so waiting on GCS does not hold one of the
    `instancia.entrar` slots that the CPU nodes compete for. After it, `duck` is not used.

    `sem_publicar=True` (root upstream with `montante_publica: false`): checks format and the
    empty guard and returns `None` without touching any destination - no object, pointer, index,
    or `export_dir`. The guard still applies because not publishing is not the same as not
    checking: an empty upstream must fail here, as it would in a normal execution, before the
    root computes over it.

    Two destinations: `arquivo` (default - `export_dir`, the usual behavior) and `lake` (silver
    layer of the medallion lake). The empty guard applies to both and comes BEFORE any write.

    Guard: does NOT overwrite a good dataset with an empty/below-minimum result - aborts the
    publication (and the node fails), preserving the previously published one. `config`:
    `{nome (slug), formato? (parquet|csv, default parquet), caminho? (dir), min_linhas? (default 1),
    permitir_vazio?, destino? (arquivo|lake), dataset? (lake), conexao_id? (lake),
    particao? (lake)}`.
    """
    cfg = no.get("config") or {}
    destino_tipo = str(cfg.get("destino") or "arquivo").lower()
    nome = cfg.get("dataset") if destino_tipo in ("lake", "iceberg") else cfg["nome"]
    formato = str(cfg.get("formato") or "parquet").lower()
    if formato not in ("parquet", "csv"):
        raise ErroAplicacao(
            codigo="saida_formato_invalido",
            mensagem=f"Output '{nome}': format '{formato}' not supported (use 'parquet' or 'csv').",
            status=400,
        )
    minimo = int(cfg.get("min_linhas", 1))
    if linhas < minimo and not cfg.get("permitir_vazio"):
        raise ErroAplicacao(
            codigo="saida_vazia",
            mensagem=(
                f"Output '{nome}' has {linhas} row(s) (< {minimo}); publication aborted "
                "so as not to overwrite the previously published data."
            ),
            status=400,
        )
    if sem_publicar:
        # Before `armazenamento.abrir`: an upstream in a run that does not publish must not move
        # the `_atual.json` that another consumer reads as frozen.
        return None
    if destino_tipo == "lake":
        # Silver/gold: the Parquet goes to a PER-EXECUTION (immutable) path and only the pointer
        # is swapped. Nothing is overwritten - the layer rewrite is the pointer's, not the data's.
        from ..ingestao import armazenamento, leitura

        lake = await armazenamento.abrir(cfg.get("conexao_id"))
        pipeline_id = await _pipeline_da_execucao(execucao_id)
        camada = str(cfg.get("camada") or "silver")
        cfg_particao = list(cfg.get("particao") or [])
        # Both DuckDB metadata reads in a single hop: they are NATIVE calls, and a native call on
        # the event loop stops the whole loop - that is what makes a 5 ms publication cost much
        # more when dozens of nodes are in flight.
        colunas, cols_part = await em_thread(_metadados_da_saida, duck, no_id, cfg_particao)
        # All the resolution (parameter, column, value becoming a path) happens before the first
        # write: the bucket is only touched after the run's whole scope has been checked.
        base = _particao_de_parametro(cfg_particao, parametros, tipos_parametro=tipos_parametro)
        escopos = await em_thread(
            _escopos_do_fanout, duck, no_id, linhas, base, cols_part, cfg_particao
        )
        # The COPY filter comes from a MAP TABLE (raw combination -> scope index), not from an OR
        # of equalities per combination. Several raw values fall into the same scope on purpose
        # (a date/time column collapses to the day), and the OR grew one term per combination:
        # rows with distinct times became a huge SQL and a very slow COPY, with the time growing
        # with the SQUARE of the rows. With the join, the SQL text is constant and the cost is a
        # hash join.
        mapa = f"_cflow_escopo_{no_id}"
        if cols_part:
            combinacoes = [
                [*crus, i] for i, (_, _, combos) in enumerate(escopos) for crus in combos
            ]
            # The whole map in one hop: DDL, building the Arrow table and INSERT. The INSERT can
            # reach tens of thousands of combinations, and on the loop it stalled every other
            # in-flight execution.
            await em_thread(_carregar_mapa, duck, mapa, cols_part, combinacoes)
        # `IS NOT DISTINCT FROM` and not `=`: a combination with NULL would never match by
        # equality and the row would silently vanish from the Parquet. (A null value is refused
        # earlier, by `valor_particao`; the join must not depend on that to be correct.)
        juncao = " AND ".join(
            f'CAST(t."{real}" AS VARCHAR) IS NOT DISTINCT FROM m.c{j}'
            for j, (_, real) in enumerate(cols_part)
        )
        # The partition columns STAY inside the Parquet (`SELECT t.*`): whoever reads a whole
        # partition, or the whole dataset by another path, cannot lose the field that defines it -
        # and DuckDB's `PARTITION_BY` is precisely what removes the column from the file.
        #
        # ONE join with the map, ordered by scope (and, within it, by the row's original position):
        # each COPY below filters `_cflow_i = ?` and the zonemap prunes the other scopes' row
        # groups. Before, each scope redid the join over the WHOLE table - O(S*n); now O(n log n)
        # once + O(row group) per scope. Each file's content is the same (the same rows).
        # A regular table, not TEMP: each scope copies through ITS cursor (below), and a cursor is
        # another connection of the same instance - it does not see a temp table from its creator.
        ordenada = f"_cflow_ordenada_{no_id}"
        if cols_part:
            await em_thread(
                duck.execute,
                f'CREATE TABLE "{ordenada}" AS SELECT t.*, m.i AS "_cflow_i" '
                f'FROM "{no_id}" t JOIN "{mapa}" m ON {juncao} ORDER BY m.i, t.rowid',
            )
        sem = asyncio.Semaphore(_CONCORRENCIA_PARTICAO)
        if (
            liberar_duck is not None
            and not cols_part
            and (_dir(execucao_id) / f"{no_id}.parquet").is_file()
        ):
            # Without a per-column fan-out, the published file IS the already-written snapshot:
            # from here on it is all network. The DuckDB slot is released now, not after the last
            # byte goes to GCS.
            await liberar_duck()
            duck = None

        async def _gravar_escopo(i: int, particao: dict, n: int, tem_filtro: bool) -> dict:
            async with sem:
                tmp_lake = _dir(execucao_id) / f".{no_id}.lake.{i}.parquet"
                if tem_filtro:
                    # One CURSOR per scope: up to `_CONCORRENCIA_PARTICAO` scopes copy at the same
                    # time, and DuckDB's Python connection is not thread-safe (the documentation
                    # asks for one cursor per thread) - before, the eight threads shared the same
                    # connection. Through `instancia.cursor`, which puts it in the node's schema: a
                    # child cursor is born in the default `search_path` and, with the shared
                    # instance, would not see the table.
                    cursor = instancia.cursor(duck)
                    try:
                        await em_thread(
                            cursor.execute,
                            f'COPY (SELECT * EXCLUDE ("_cflow_i") FROM "{ordenada}" '
                            f"WHERE \"_cflow_i\" = ?) TO '{tmp_lake}' (FORMAT parquet)",
                            [i],
                        )
                    finally:
                        cursor.close()
                    arquivo = tmp_lake
                else:
                    # Without a scope filter, the published file IS the node's snapshot: the same
                    # `COPY "{no_id}" ... (FORMAT parquet)` that `_gravar_parquet_atomico` just
                    # wrote (the only caller publishes after it). A second COPY was one more full
                    # Parquet encoding of the same data per output. READ only: the snapshot is
                    # immutable and remains what the next node reads. Without a snapshot (direct
                    # call, outside `executar_no`), the COPY as before.
                    arquivo = _dir(execucao_id) / f"{no_id}.parquet"
                    if not arquivo.is_file():
                        await em_thread(
                            duck.execute, f"COPY \"{no_id}\" TO '{tmp_lake}' (FORMAT parquet)"
                        )
                        arquivo = tmp_lake
                try:
                    return await leitura.gravar_dataset(
                        lake=lake,
                        dataset=nome,
                        execucao_id=execucao_id,
                        pipeline_id=pipeline_id,
                        no_id=no_id,
                        # The PATH, not the bytes: the local backend publishes by `os.link` and the
                        # Parquet does not pass through the process memory. It used to be up to
                        # `_CONCORRENCIA_PARTICAO` whole copies of the dataset in RAM at the same
                        # time, one per in-flight scope.
                        parquet=Path(arquivo),
                        linhas=n,
                        colunas=colunas,
                        camada=camada,
                        particao=particao or None,
                        parametros=parametros,
                        tipos_parametro=tipos_parametro,
                    )
                finally:
                    tmp_lake.unlink(missing_ok=True)

        # TWO PHASES, and that is what gives the fan-out a commit point. Phase 1 writes all the
        # data to an immutable path (which no consumer sees); only phase 2 moves the pointers.
        #
        # `return_exceptions=True` because a bare `gather` propagates the 1st exception and does
        # NOT cancel the siblings: the node was marked 'failed', the index was never written, and
        # seconds later the other scopes finished moving their `_atual.json` on the worker's event
        # loop - the execution showed as failed and the bucket said otherwise. Waiting for ALL and
        # only then deciding is what makes "half published is worse than nothing published" hold
        # for WRITE failures too, not only for value validation. A single GCS 5xx on one scope is
        # enough to trigger it.
        resultados = await asyncio.gather(
            *(
                _gravar_escopo(i, particao, n, bool(cols_part))
                for i, (particao, n, _) in enumerate(escopos)
            ),
            return_exceptions=True,
        )
        _propagar_falha_do_fanout(resultados, escopos)
        publicados = list(resultados)

        # Phase 2: the *commit point*. It also waits for all - a failure here leaves part of the
        # run visible, but the object is small and already written; aborting midway without
        # waiting would leave the siblings' writes running after the node failed, which is
        # exactly the defect above.
        async def _mover_ponteiros() -> None:
            movidos = await asyncio.gather(
                *(
                    leitura.mover_ponteiro(lake=lake, camada=camada, publicado=pub)
                    for pub in publicados
                ),
                return_exceptions=True,
            )
            _propagar_falha_do_fanout(movidos, escopos)

        if not cfg_particao:
            await _mover_ponteiros()
            return publicados[0]["uri"]
        sem_prefixo = [{k: v for k, v in pub.items() if k != "prefixo"} for pub in publicados]
        # With a partition, `<dataset>/_atual.json` stops being "the latest parquet" (which of the
        # scopes?) and becomes the index of this run. It is what is read from outside without
        # listing the bucket.
        #
        # The scope pointers and the read of `_atual.json` (under the lock) run TOGETHER; then
        # the immutable index, and `_atual.json` last. The order that is a contract does not
        # change: data before pointer, pointer before immutable, immutable before mutable. With
        # data and manifest together (`gravar_dataset`), publishing goes from 6 serial trips to 4.
        return await leitura.publicar_indice(
            lake=lake,
            dataset=nome,
            camada=camada,
            execucao_id=execucao_id,
            pipeline_id=pipeline_id,
            no_id=no_id,
            parametros=parametros,
            # From the CONFIG, not the first scope: with `permitir_vazio` the fan-out produces no
            # scope at all, and the index still has to say what the path format is.
            chaves=[it.get("chave") or it["nome"].lower() for it in cfg_particao],
            # The NODE's, read before the fan-out: with zero scopes there is no manifest to say.
            colunas=colunas,
            prefixo_imutavel=_prefixo_imutavel(
                nome,
                cfg_particao,
                base,
                camada=camada,
                ambiente=parametros.get("ambiente"),
            ),
            tipos_parametro=tipos_parametro,
            particoes=[
                {
                    "particao": p["particao"],
                    "caminho": p["caminho"],
                    "linhas": p["linhas"],
                    "bytes": p["bytes"],
                }
                for p in sem_prefixo
            ],
            esperar=_mover_ponteiros(),
        )

    if destino_tipo == "iceberg":
        # Silver as an Iceberg TABLE: query engines read it natively, and typing a column no
        # longer requires rewriting the dataset. Each execution becomes a snapshot; earlier
        # versions remain queryable via time travel.
        from ..ingestao import armazenamento, iceberg, tokenizacao

        lake = await armazenamento.abrir(cfg.get("conexao_id"))
        pipeline_id = await _pipeline_da_execucao(execucao_id)
        # `to_arrow_table()` and NOT `arrow()`: the latter returns a RecordBatchReader, which
        # PyIceberg refuses ("Expected PyArrow table"). It materializes in memory - the known
        # cost of PyIceberg, which does not accept a stream; several times the Parquet size.
        tabela_arrow = await em_thread(
            lambda: duck.execute(f'SELECT * FROM "{no_id}"').to_arrow_table()
        )
        publicado = await iceberg.publicar(
            lake=lake,
            dataset=nome,
            tabela_arrow=tabela_arrow,
            # `config.grao` of the output node, written by `gerador_silver.grao_para_saida`.
            # Without passing it here `conferir_grao` reads `desconhecido` and returns without
            # blocking - the historical defect of `guardas_sql`: a function covered by a test that
            # production never calls.
            grao=cfg.get("grao"),
            # Same reasoning as `grao` above, and the same historical defect to avoid: without
            # passing the tokenizer here, `tokenizar_tabela` would receive `None` and REFUSE to
            # publish tables with sensitive columns - or, worse in a future version, publish them
            # in the clear. The lake backend knows how to build the tokenizer, because the KMS key
            # is reached with the token it already obtains from the vault.
            tokenizador=await tokenizacao.abrir_tokenizador(lake),
            propriedades={
                "cflow.execucao_id": execucao_id,
                "cflow.pipeline_id": pipeline_id,
                "cflow.no_id": no_id,
            },
        )
        return publicado["metadata_location"]

    destino = Path(cfg.get("caminho") or get_settings().export_dir)
    destino.mkdir(parents=True, exist_ok=True)
    final = destino / f"{nome}.{formato}"
    # Temp file PER EXECUTION. With a fixed name (`.{nome}.{formato}.tmp`), two executions of the
    # same pipeline writing the same `nome` wrote to the SAME temp file: the second COPY went over
    # the first's file and the first's `os.replace` published mixed bytes. With the
    # `execucao_id` in the name, the worst case becomes "the last to finish wins" - atomic,
    # never corrupted.
    tmp = destino / f".{nome}.{formato}.{execucao_id or uuid.uuid4().hex}.tmp"
    copia = "(FORMAT csv, HEADER)" if formato == "csv" else "(FORMAT parquet)"
    await em_thread(duck.execute, f"COPY \"{no_id}\" TO '{tmp}' {copia}")
    os.replace(tmp, final)
    return str(final)


# --- Common guards for EFFECT nodes (escrita_sql / http): idempotency and re-entry fail-safe.
# Both nodes stay OUTSIDE incremental reuse (they always run), but still must not repeat the
# effect on a Temporal activity redelivery. The two guards below are shared.


async def _reuso_efeito_concluido(execucao_id, no_id: str, snap) -> dict | None:
    """IDEMPOTENCY: if this effect node ALREADY completed in this execution with a snapshot on
    disk (activity redelivery after an external commit, pre-ack), returns the reuse dict - the
    caller returns without repeating the effect. Otherwise, None."""
    async with transacao() as conn:
        ja = (
            (
                await conn.execute(
                    text(
                        "SELECT snapshot, linhas, hash FROM execucao_nos "
                        "WHERE execucao_id = :e AND no_id = :n AND status = 'concluida' "
                        "AND snapshot IS NOT NULL"
                    ),
                    {"e": execucao_id, "n": no_id},
                )
            )
            .mappings()
            .first()
        )
    if ja is not None and os.path.isfile(ja["snapshot"]):
        if ja["snapshot"] != str(snap):
            await em_thread_lake(_copiar_atomico, ja["snapshot"], snap)  # I/O, not CPU work
        return {
            "no_id": no_id,
            "linhas": ja["linhas"],
            "hash": ja["hash"],
            "snapshot": str(snap),
            "duracao_ms": 0,
            "reaproveitado": True,
        }
    return None


async def _marcar_efeito_ou_recusar(execucao_id, no_id: str, *, idempotente: bool) -> None:
    """RE-ENTRY FAIL-SAFE: closes the window between the EXTERNAL commit and the 'concluida'
    upsert (the idempotency guard above only covers nodes ALREADY completed). Writes the durable
    `efeito_disparado` mark in its own transaction BEFORE the effect; the failure mark (except)
    does NOT erase it. On a redelivery/retry, if the mark exists and the node did not complete,
    the effect MAY have committed externally: NON-idempotent operations (append/sql/http) are
    REFUSED instead of duplicating; the idempotent ones (upsert/truncate_insert) may safely
    repeat."""
    async with transacao() as conn:
        disparado = (
            await conn.execute(
                text(
                    "SELECT efeito_disparado FROM execucao_nos WHERE execucao_id = :e AND no_id = :n"
                ),
                {"e": execucao_id, "n": no_id},
            )
        ).scalar_one_or_none()
        recusar = bool(disparado) and not idempotente
        if not recusar:
            await conn.execute(
                text(
                    "INSERT INTO execucao_nos (execucao_id, no_id, status, efeito_disparado) "
                    "VALUES (:e, :n, 'executando', true) "
                    "ON CONFLICT (execucao_id, no_id) DO UPDATE SET efeito_disparado = true"
                ),
                {"e": execucao_id, "n": no_id},
            )
    if recusar:
        raise ErroAplicacao(
            codigo="escrita_reentrada",
            mensagem=(
                f"Node '{no_id}': the effect was already triggered in an earlier attempt that did "
                "not complete. A non-idempotent operation is not re-run automatically - check the "
                "destination before re-running."
            ),
            status=409,
        )


class _FilaDeMarcas:
    """Groups the "started" marks of SEVERAL nodes into a single statement.

    Measured with `CFLOW_BENCH_FASES=1`: hundreds of marks added up to **seconds** (~28 ms each)
    - the largest single item of state waiting in a run whose whole node time is a few tens of
    seconds. For a node that lasts 50 ms, its mark cost half of that.

    The mark is pure observability: what releases the dependent is memory
    (`paralelo.rodar_por_dependencia`), not the database. So it can wait - what it CANNOT do is
    arrive after an outcome write of the same node and erase it. Hence the design: enqueueing is
    instantaneous, and `descarregar()` is called by `_marcado()`, which was already the mandatory
    point before any other write of the node. While a node works, its siblings' marks pile up;
    when it is about to write, it takes them all - with ~12 nodes in flight, the grouping is real.

    An earlier attempt merely MOVED the source's mark to an `ensure_future`, without grouping:
    it lost on all three pairs. There is no overlap to gain, only task cost and pool contention.
    What solves it is grouping.

    Single flight on flush: N nodes calling `descarregar()` at the same time would make N
    one-row statements, which is exactly what we want to avoid. Whoever arrives during a flush in
    progress waits for the NEXT one, because the current one may not contain its mark.
    """

    def __init__(self) -> None:
        self._pendentes: dict[tuple[str, str], tuple] = {}
        self._voo: asyncio.Future | None = None

    def enfileirar(self, execucao_id, no_id: str) -> None:
        self._pendentes[(str(execucao_id), no_id)] = (execucao_id, no_id)

    async def descarregar(self) -> None:
        if self._voo is not None:
            # A flush in progress may have been assembled BEFORE my mark entered: I wait for it
            # and move on to my own, which takes what is left.
            await asyncio.shield(self._voo)
        if not self._pendentes:
            return
        self._voo = asyncio.ensure_future(self._gravar())
        try:
            await asyncio.shield(self._voo)
        finally:
            self._voo = None

    async def _gravar(self) -> None:
        lote, self._pendentes = list(self._pendentes.values()), {}
        if not lote:
            return
        async with autocommit() as conn:
            # `executemany`: asyncpg sends the binds in a single round trip, over the same prepared
            # statement. Distinct nodes do not collide with each other in the `ON CONFLICT`.
            await conn.execute(_SQL_MARCAR_INICIADO, [{"e": e, "n": n} for e, n in lote])


FILA_MARCAS = _FilaDeMarcas()


async def _marcar_no_iniciado(execucao_id: uuid.UUID, no_id: str, *, agrupar: bool = False) -> None:
    """Mark the node as `executando` BEFORE running it - this is what the UI reads to say WHICH
    node is in progress.

    Without this row, `execucao_nos` only existed from the OUTCOME (completed/failed): during
    execution, the node running NOW was indistinguishable from one that had not even started, and
    the detail showed "waiting" for both. In a graph of 24 nodes with 20-minute stages, that is
    the difference between "it's moving" and "it's stuck".

    The `WHERE status <> 'concluida'` is not a style detail: activity redelivery depends on
    finding the node ALREADY completed (idempotency guard and reuse). Rewriting the row to
    `executando` here would erase that evidence and make an external effect repeat.
    """
    if agrupar:
        # Goes to the queue; `_marcado()` writes it, at the point where order starts to matter.
        FILA_MARCAS.enfileirar(execucao_id, no_id)
        return
    async with autocommit() as conn:  # a single statement: BEGIN/COMMIT would protect nothing
        await conn.execute(_SQL_MARCAR_INICIADO, {"e": execucao_id, "n": no_id})


_MARCAR_INICIADO = (
    "INSERT INTO execucao_nos (execucao_id, no_id, status, iniciado_em) "
    "VALUES (:e, :n, 'executando', now()) "
    "ON CONFLICT (execucao_id, no_id) DO UPDATE SET "
    "status='executando', iniciado_em=now(), erro=NULL "
    "WHERE execucao_nos.status <> 'concluida'"
)
_SQL_MARCAR_INICIADO = text(_MARCAR_INICIADO)
#: Derived node: the mark and the lookup for an equal snapshot in a single statement. The mark
#: goes in a CTE, which Postgres runs in full even if it is not read; the SELECT does not see the
#: row it writes (same statement snapshot), and it would never be a candidate - it is not
#: `concluida`.
_SQL_MARCAR_E_BUSCAR = text(
    "WITH marca AS (" + _MARCAR_INICIADO + ") "
    "SELECT snapshot, hash, linhas FROM execucao_nos "
    "WHERE fingerprint = :fp AND status = 'concluida' AND snapshot IS NOT NULL "
    "ORDER BY concluido_em DESC LIMIT 5"
)

#: Nodes with incremental reuse by fingerprint (see `executar_no`).
_DERIVADOS = ("sql", "guiada", "comparacao", "python")


def _recortar_para_a_fonte(definicao_fonte: dict, ctx: dict) -> dict | None:
    """The `ctx` keys that the source pipeline declares, used to filter its "latest run".

    `ctx` is already NORMALIZED (it is what was stored in `execucoes.parametros`), so this is a
    pure slice: re-normalizing would push the value through rules that assume the RAW value.
    The case that forces a look at the TYPE is `competencia`: in the normalized context it does
    not exist under its own name; it became `{nome}_inicio` and `{nome}_fim`.

    Returns `None` when nothing matches. That is intent hygiene, **not a safeguard**: in
    `resolver_snapshot` the test is `if parametros:`, so `{}` and `None` take the SAME path (the
    unfiltered one) and both return the latest run of any as-of. Filtering by `{}` would filter
    by nothing anyway, because `@>` with an empty object matches every run. What closes the gap
    is the source pipeline DECLARING the cut parameters; a source that declares none keeps
    serving the latest run of any as-of, and no `None` here changes that.

    This path only exists when the chain arrives INCOMPLETE. With an explicit `fontes_execucao`,
    `resolver_snapshot` takes the `execucao_id` branch, which matches by id and **ignores the
    filter**.
    """
    fora: dict = {}
    for p in parametros_declarados(definicao_fonte):
        if not isinstance(p, dict) or not isinstance(p.get("nome"), str):
            continue
        nome = p["nome"]
        chaves = (f"{nome}_inicio", f"{nome}_fim") if p.get("tipo") == "competencia" else (nome,)
        for chave in chaves:
            if chave in ctx:
                fora[chave] = ctx[chave]
    return fora or None


async def executar_no(
    *,
    execucao_id: uuid.UUID,
    definicao: dict,
    no_id: str,
    parametros: dict | None = None,
    fontes_execucao: dict | None = None,
    forcar: bool = False,
    sem_publicar: bool = False,
) -> dict:
    """Run ONE isolated node: register the predecessors' snapshots (Parquet), run the SAME SQL
    as the preview and write the node's own snapshot plus its trail row. Self-contained, so it is
    the body of one Temporal *activity* (one node = one activity). Precondition: the
    predecessors' snapshots already exist.

    `fontes_execucao` ({pipeline_id -> execucao_id}): in an A->B chain, forces the `pipeline`
    source to read the snapshot of the EXACT run of A made just now (freshness) instead of the
    latest published one. Absent, or without the pipeline in the map: normal behaviour
    (version/latest).

    `forcar=True` DISABLES fingerprint reuse on this node: it recomputes even if an identical
    snapshot exists. This is what backs "run from here": someone asking to redo a node that
    finished (with suspect data, or after the external source changed) would otherwise get back
    exactly the snapshot they wanted to discard.

    `sem_publicar=True` (upstream of a trigger whose root declares `montante_publica: false`):
    the node runs and writes its snapshot normally (the root reads it through the freshness
    map), but its output is not published (`_publicar_saida`). It is an explicit argument, never
    a mutated definition: the definition is the published version and must keep saying what ran.
    """
    nos = {n["id"]: n for n in definicao.get("nos", [])}
    no = nos[no_id]
    tipo = no.get("tipo")
    dir_exec = _dir(execucao_id)
    dir_exec.mkdir(parents=True, exist_ok=True)
    preds = [a["de"] for a in definicao.get("arestas", []) if a["para"] == no_id]

    snap = dir_exec / f"{no_id}.parquet"
    # The DuckDB instance is only created when the node actually uses it (`_preparar_duck`,
    # below): a node that exits through reuse or the chain cache does not pay to create, set up
    # and tear down an instance.
    duck = None
    slot = None  # connection owner: the node's own instance, or a cursor+schema on the process one
    t0 = time.monotonic()

    async def _sql(sql: str, params=None):
        """Heavy DuckDB command, run off the event loop (see `bloqueante`). The connection
        belongs to this node and is used by one thread at a time."""
        return await em_thread(duck.execute, sql, params)

    voo_extracao = None  # key of the extraction's single flight, when this node holds it
    marca: asyncio.Future | None = None  # in-flight "started" mark (source/output), see `_marcado`

    async def _marcado() -> None:
        """The start mark must be WRITTEN before any other write of the node: its upsert only
        spares a `concluida` row, so arriving after a `falha` it would erase it."""
        nonlocal marca
        if marca is not None:
            m, marca = marca, None
            await m
        # Write the batched marks (this node's and those of siblings that joined meanwhile).
        # It happens here, not earlier, because this is where the order starts to matter.
        await FILA_MARCAS.descarregar()

    try:
        # First write of the node: "started", so the UI shows the node running during ALL the
        # time it runs, including the source read, which is usually the long step. Measured
        # with per-phase instrumentation (`CFLOW_BENCH_FASES=1`): it was the single largest
        # state-wait item of a run, for a node that sometimes lasts 50 ms. Derived nodes: the
        # mark joins the reuse CTE below and costs no extra statement. Output: runs ALONGSIDE the
        # work and is awaited in `_marcado()` before the next write. Source, effect and the rest
        # mark before anything else.
        #
        # Firing the SOURCE mark in the background was also tried and MEASURED: it lost on every
        # pair (about +3.7% wall time). There is no overlap to gain, since `_marcado()` is
        # awaited right after in chain reuse, and it adds task creation and pool contention. What
        # fixes this cost is BATCHING the writes, not advancing them one by one.
        if tipo == "saida":
            marca = asyncio.ensure_future(_marcar_no_iniciado(execucao_id, no_id))
        elif tipo == "fonte":
            # BATCHED: it joins the queue and is written together with its siblings' marks, in
            # `_marcado()`. It still goes through `_marcar_no_iniciado` on purpose: that is the
            # point where the concurrent-chain test interleaves the other run, and moving the hook
            # would make it pass without testing anything.
            await _marcar_no_iniciado(execucao_id, no_id, agrupar=True)
        elif tipo not in _DERIVADOS:
            await _marcar_no_iniciado(execucao_id, no_id)
        # Load the MACROs of the libraries PINNED in this run: same DDL as the preview, available
        # before any node SQL runs. Cheap (CREATE OR REPLACE) and unconditional; an empty pin is a
        # no-op. Reproducibility: the exact version, not "the latest published".
        fatos = await _fatos_da_execucao(execucao_id)
        pin = fatos["bibliotecas_versoes"] if fatos else None
        cadeia_id = fatos["cadeia_id"] if fatos else None
        iniciada_por = fatos["iniciada_por"] if fatos else None
        em_cache = bf_aplicar.ddl_em_cache(pin or [])
        if em_cache is not None:
            ddl_macros, nomes_macros = em_cache
        else:
            async with transacao() as conn:
                ddl_macros, nomes_macros = await bf_aplicar.ddl_para_execucao(conn, pin or [])
        # MACROs only go into a node whose text CITES one (the node SQL, or the guided condition
        # in `config`): a connection source runs its SQL at the origin, and output/passthrough
        # nodes call no function. Outside the transaction and the loop: the DDL is dozens of
        # CREATE MACRO. When one is cited, all are loaded: a MACRO may call another.
        aplicar_macros = bool(ddl_macros) and bf_aplicar.cita_macro(
            json.dumps({"sql": no.get("sql"), "config": no.get("config")}, default=str),
            nomes_macros,
        )
        aliases = aliases_de_rotulos(definicao)

        def _abrir(vaga, apos=None):
            # ONE hop to the thread: the slot (own instance, or a cursor with its own schema on the
            # process instance, see `instancia.abrir`), the MACROs when the node cites any, and the
            # inputs: each predecessor's snapshot as a VIEW over `read_parquet` (durable data
            # handoff; the view only reads the file footer). `apos` is the node's first command
            # (the `sql` CTAS), in the same hop.
            s = instancia.abrir("execucao", vaga)
            con = s.con
            try:
                if aplicar_macros:
                    bf_aplicar.aplicar_ddl(con, ddl_macros)
                for p in preds:
                    snap_p = dir_exec / f"{p}.parquet"
                    con.execute(f"CREATE VIEW \"{p}\" AS SELECT * FROM read_parquet('{snap_p}')")
                    if p in aliases:  # label as alias (same rule as the preview, see alias.py)
                        criar_alias(con, p, aliases[p])
                if apos is not None:
                    apos(con)
            except BaseException:
                s.fechar()
                raise
            return s

        async def _preparar_duck(apos=None) -> None:
            nonlocal duck, slot
            if duck is None:
                # The slot is awaited in the LOOP, before going to the thread: the cap on open
                # DuckDB instances is PROCESS-wide (`instancia.entrar`), and waiting for it in an
                # executor thread would burn an executor slot doing nothing. It is returned by
                # `Slot.fechar`, including on the error path of `_abrir`.
                vaga = await instancia.entrar()
                try:
                    slot = await em_thread(_abrir, vaga, apos)
                except BaseException:
                    vaga.sair()
                    raise
                duck = slot.con
            elif apos is not None:
                await em_thread(apos, duck)

        # INCREMENTAL REUSE (derived nodes): if the SQL/op and the input content did not change,
        # reuse the snapshot of an earlier run instead of recomputing. Sources always re-run.
        fp = None
        chave_cadeia = None  # (chain, fp) of the source extraction: feeds `_SNAP_DA_CADEIA`
        evento_efeito = (
            None  # effect (escrita_sql/http/email) -> `escrita_externa` record in the tail
        )
        evento_leitura = None  # lake source -> `leitura_lake` record (bronze->silver lineage)
        snap_repasse = None  # pure passthrough output: snapshot = link to the predecessor's Parquet
        arquivo_repasse = None  # source passthrough: the file read, key of `_HASH_ARQUIVO`
        if tipo in _DERIVADOS:
            # Only the hashes of the PREDECESSORS (reading every node's hash in the run for each
            # node was O(N) rows per node, O(N^2) per pipeline). Those this process finished come
            # from memory, validated against the snapshot this node will read; only the missing
            # ones go to the database.
            hashes: dict = {}
            faltam = []
            for p in preds:
                h_p = _hash_lembrado(execucao_id, p, dir_exec / f"{p}.parquet")
                if h_p is None:
                    faltam.append(p)
                else:
                    hashes[p] = h_p
            # Autocommit: the mark is an idempotent statement and the rest are reads (READ
            # COMMITTED, same visibility as before); the transaction only cost BEGIN/COMMIT.
            async with autocommit() as conn:
                if faltam:
                    hashes.update(
                        {
                            r["no_id"]: r["hash"]
                            for r in (
                                await conn.execute(
                                    text(
                                        "SELECT no_id, hash FROM execucao_nos "
                                        "WHERE execucao_id = :e AND no_id = ANY(:preds)"
                                    ),
                                    {"e": execucao_id, "preds": faltam},
                                )
                            )
                            .mappings()
                            .all()
                        }
                    )
                fp = _fingerprint(no, [hashes.get(p, "") for p in preds], pin or [], parametros)
                # `forcar`: the fingerprint is still COMPUTED (it is stored on completion and
                # serves reuse in FUTURE runs); what is disabled is the SEARCH for an equal
                # snapshot. Not computing fp here would leave the node without identity and drop a
                # node that just ran out of future reuse.
                if forcar:
                    await conn.execute(_SQL_MARCAR_INICIADO, {"e": execucao_id, "n": no_id})
                    cand = []
                else:
                    cand = (
                        (
                            await conn.execute(
                                _SQL_MARCAR_E_BUSCAR, {"e": execucao_id, "n": no_id, "fp": fp}
                            )
                        )
                        .mappings()
                        .all()
                    )
                for c in cand:
                    if not os.path.isfile(c["snapshot"]):
                        continue
                    if c["snapshot"] != str(snap):
                        # bring it into this run (downstream reads from here)
                        await _trazer_snapshot(c["snapshot"], snap)
                    # On the SAME connection as the search: one pool checkout (and pre-ping) less.
                    await conn.execute(
                        text(
                            "INSERT INTO execucao_nos (execucao_id, no_id, status, linhas, hash, snapshot, duracao_ms, fingerprint, reaproveitado, concluido_em) "
                            "VALUES (:e, :n, 'concluida', :l, :h, :s, 0, :fp, true, now()) "
                            "ON CONFLICT (execucao_id, no_id) DO UPDATE SET "
                            "status='concluida', linhas=:l, hash=:h, snapshot=:s, duracao_ms=0, "
                            "fingerprint=:fp, reaproveitado=true, erro=NULL, concluido_em=now()"
                        ),
                        {
                            "e": execucao_id,
                            "n": no_id,
                            "l": c["linhas"],
                            "h": c["hash"],
                            "s": str(snap),
                            "fp": fp,
                        },
                    )
                    _lembrar_no(execucao_id, no_id, c["hash"], snap)
                    return {
                        "no_id": no_id,
                        "linhas": c["linhas"],
                        "hash": c["hash"],
                        "snapshot": str(snap),
                        "duracao_ms": 0,
                        "reaproveitado": True,
                    }

        # From here on the node uses DuckDB, except the CONNECTION source, which opens the
        # instance only after the chain cache (branch below): a node that exits through the cache
        # never opens it. The `sql` node opens it together with its CTAS, in a single hop.
        origem_cfg = (no.get("config") or {}).get("origem") or "conexao"
        # The source opens the instance INSIDE its branch, after trying chain reuse: a node that
        # exits through reuse never opens it (and never takes one of the `instancia.entrar` slots).
        # Only the `lake` source still prepares here; its reuse was not measured.
        if tipo != "sql" and not (
            tipo == "fonte" and origem_cfg in ("conexao", "publicado", "pipeline")
        ):
            await _preparar_duck()
        if tipo == "fonte":
            cfg_no = no["config"]
            origem_no = cfg_no.get("origem") or "conexao"
            if origem_no == "publicado":
                # Source = ARTIFACT published by a `saida` node (silver/gold). It does not re-run the
                # publisher. Outside a chain (or without the producer in it), it reads the scope's
                # POINTER: several loads on the same day converge on the latest, which is the
                # current pointer.
                #
                # With the producer IN the chain, the pointer is the wrong answer: it is partitioned
                # by scope only, and another chain of the same scope with a different as-of moves it
                # between this one's publication and its read, so the run would come out green with
                # two as-ofs. The read becomes the publication of THAT run, through its immutable
                # index, when the requested scope is the one it produces (frozen history of another
                # period still comes from the pointer, see `resolver_publicado_na_cadeia`).
                from ..ingestao import leitura

                lido = await leitura.resolver_publicado_na_cadeia(
                    cfg_no, fontes_execucao, excluir=execucao_id, parametros=parametros
                )
                if lido is None:
                    lido = await leitura.resolver_publicado(cfg_no, parametros=parametros)
                ponteiro, caminhos = lido
                # Lineage: WHICH publication was read. Without the producer's `execucao_id`,
                # redoing the read tomorrow would return a different parquet and the trace would not
                # close. Built HERE, before any read, because the reuse path below must also record
                # it: a reused node without lineage is a hole in the trail.
                evento_leitura = {
                    "dataset": ponteiro.get("dataset"),
                    "camada": ponteiro.get("camada"),
                    "execucao_id_origem": ponteiro.get("execucao_id"),
                    "momento_publicacao": ponteiro.get("momento"),
                    "caminhos": caminhos,
                }
                sql_p = cfg_no.get("sql")
                # CHAIN REUSE, like the connection source: a sibling already read THESE paths and its
                # snapshot serves. Only for PURE PASSTHROUGH (no SELECT): the node SQL cites the alias,
                # which carries the `no_id`, so two nodes "with the same SQL" have different texts and
                # would never match; and passthrough is where the cost is (the same dataset read many
                # times in one run). The paths are the fingerprint INPUTS: each carries `execucao_id=`,
                # so it is immutable.
                if cadeia_id is not None and not forcar and not (sql_p and sql_p.strip()):
                    fp_cadeia = _fingerprint(no, sorted(caminhos), pin or [], parametros)
                    chave_cadeia = (str(cadeia_id), fp_cadeia)
                    # SINGLE FLIGHT before reuse, like the connection source: the cache only sees
                    # COMPLETED nodes, and siblings that start together (measured: 11 ms apart) would
                    # both read. The second waits for the first and exits through the cache. Before
                    # the `instancia.entrar` slot, never after: that order prevents the deadlock (see
                    # the docstring of `instancia.entrar`).
                    voo_extracao = await VOO_EXTRACAO.entrar(chave_cadeia)
                    reuso = await _reuso_da_cadeia(
                        chave_cadeia,
                        execucao_id=execucao_id,
                        no_id=no_id,
                        snap=snap,
                        cadeia_id=cadeia_id,
                        fp=fp_cadeia,
                        marcar=_marcado,
                        evento_leitura=evento_leitura,
                        iniciada_por=iniciada_por,
                    )
                    if reuso is not None:
                        return reuso
                await (
                    _preparar_duck()
                )  # only now: a node that exits through reuse never opens the instance
                alias = f"fonte_{no_id}"
                # `opcional` source whose scope was never published (the period that did not close):
                # the table comes out EMPTY with the dataset index's columns, all VARCHAR. The type
                # does not matter in a table without rows; what matters is that the column exists,
                # so the reader's SQL compiles. See `leitura._escopo_opcional_vazio`.
                escopo_ausente = not caminhos and bool(ponteiro.get("escopo_ausente"))
                if escopo_ausente:
                    colunas_vazias = ", ".join(f'"{c}" VARCHAR' for c in ponteiro["colunas"])
                    await _sql(f'CREATE TABLE "{alias}" ({colunas_vazias})')
                else:
                    lista = ", ".join(f"'{c}'" for c in caminhos)
                    # VIEW, not TABLE: the node table right below already materializes. Copying
                    # everything into the alias was an extra pass and an O(n*c) copy, and without it
                    # DuckDB pushes the node SQL's projection and filter down into `read_parquet`. The
                    # file read is immutable (per-run path), so reading through the view reads the
                    # same data.
                    await _sql(f'CREATE VIEW "{alias}" AS SELECT * FROM read_parquet([{lista}])')
                if escopo_ausente:
                    await _sql(
                        f'CREATE TABLE "{no_id}" AS '
                        + (sql_p if sql_p and sql_p.strip() else f'SELECT * FROM "{alias}"')
                    )
                elif sql_p and sql_p.strip():
                    await _sql(f'CREATE TABLE "{no_id}" AS {sql_p}')
                elif len(caminhos) == 1 and os.path.isfile(caminhos[0]):
                    # Artifact passthrough, like the output and the pipeline source: the node IS the
                    # published parquet. A VIEW instead of a materialized table, because the same
                    # dataset may be read many times in a run, which meant reading, materializing and
                    # rewriting the same bytes each time.
                    await _sql(f'CREATE VIEW "{no_id}" AS SELECT * FROM "{alias}"')
                    arquivo_repasse = caminhos[0]
                    # The snapshot only becomes a LINK when the node IS the file COLUMN FOR COLUMN.
                    # The lake read is hive-partitioned: `.../scope_id=1/execucao_id=<uuid>/dados.parquet`
                    # adds columns that are NOT inside the parquet. Linking there would hand the
                    # consumer a snapshot with fewer columns than the node computed: the node hash
                    # would be right and the file would not.
                    if await em_thread(_colunas_iguais, duck, no_id, caminhos[0]):
                        snap_repasse = Path(caminhos[0])
                else:
                    await _sql(f'CREATE TABLE "{no_id}" AS SELECT * FROM "{alias}"')
            elif origem_no == "objeto":
                # Source = ONE read-only layer file (legacy `data_locked`), at the path built from
                # the parameters. Absent with `opcional` = empty table with the declared columns; in
                # `existencia` mode, a single row if the file exists.
                from ..ingestao import objeto as fonte_objeto

                lido = await fonte_objeto.resolver(cfg_no, parametros=parametros)
                alias = f"fonte_{no_id}"
                await _sql(f'CREATE TABLE "{alias}" AS {fonte_objeto.sql_relacao(lido, cfg_no)}')
                sql_p = cfg_no.get("sql")
                await _sql(
                    f'CREATE TABLE "{no_id}" AS '
                    + (sql_p if sql_p and sql_p.strip() else f'SELECT * FROM "{alias}"')
                )
                # Lineage: WHICH file (and which content) was read. The legacy file is rewritten at
                # the same path, so the address alone does not reconstruct the read.
                evento_leitura = {
                    "objeto": lido["uri"],
                    "camada": lido["camada"],
                    "existe": lido["existe"],
                    "sha256": lido["sha256"],
                }
            elif origem_no == "lake":
                # Source = a LAKE dataset (bronze). Which parquets to read comes from the index
                # (`ingestao_lotes`), not from a glob on the bucket: the index knows which version of
                # each slice is valid when the origin republishes the year's file.
                from ..ingestao import leitura

                partes_lake, resumo_lake = await leitura.partes(cfg_no, parametros=parametros)
                alias = f"fonte_{no_id}"
                sql_lake, binds = leitura.sql_leitura(
                    partes_lake, competencia=resumo_lake.get("fatia_e_competencia", False)
                )
                await _sql(f'CREATE TABLE "{alias}" AS {sql_lake}', binds)
                sql_p = cfg_no.get("sql")
                await _sql(
                    f'CREATE TABLE "{no_id}" AS '
                    + (sql_p if sql_p and sql_p.strip() else f'SELECT * FROM "{alias}"')
                )
                # Lineage: without recording WHICH batches were read it cannot be rebuilt, since new
                # batches arrive later and redoing the resolution would return another set.
                evento_leitura = {
                    **resumo_lake,
                    "lotes": [{"chave": p.chave, "sha256": p.sha256} for p in partes_lake],
                }
            elif origem_no == "pipeline":
                # Source = output of ANOTHER pipeline: reads the Parquet snapshot of the referenced
                # run (exact version). Deterministic; the snapshot is a pure function of the source
                # run. In a chain, `fontes_execucao` points to the EXACT run of A made just now.
                pid_fonte = str(cfg_no["pipeline_id"])
                exec_fonte = (fontes_execucao or {}).get(pid_fonte)
                async with transacao() as conn:
                    # WITHOUT freshness (outside the chain), "the latest completed" would be that of
                    # ANY as-of. We slice to what the source pipeline DECLARES: a SLICE, not a
                    # re-normalization. `parametros` here is ALREADY the normalized, redacted context
                    # of the current run, and passing it back through `normalizar_parametros` broke
                    # scheduling. A `competencia` parameter disappears from the normalized context
                    # (it becomes `{nome}_inicio`/`{nome}_fim`), so `filtrar_parametros_declarados`
                    # dropped both and validation reported "required missing"; with a dynamic
                    # default it was worse, the filter pointed at the CURRENT month. The manual route
                    # did not see it because it pre-creates the whole chain; the scheduler
                    # (`_dispatch`, no `fontes_execucao`) and re-runs did.
                    params_fonte: dict | None = None
                    if exec_fonte is None and parametros:
                        det_fonte = await ps.obter(
                            conn, cfg_no["pipeline_id"], cfg_no.get("versao")
                        )
                        params_fonte = _recortar_para_a_fonte(det_fonte["definicao"], parametros)
                    origem = await resolver_snapshot(
                        conn,
                        cfg_no["pipeline_id"],
                        cfg_no["no_id"],
                        execucao_id=exec_fonte,
                        versao_numero=None if exec_fonte else cfg_no.get("versao"),
                        parametros=params_fonte,
                    )
                # Materialize the upstream output under a stable alias (`fonte_{no_id}`) and, if the
                # node has SQL, run the user's SELECT on top; without SQL, pass the whole output
                # through. An alias derived from the node id is unique in the graph, no collision.
                alias = f"fonte_{no_id}"
                snap_origem = origem[
                    "snapshot"
                ]  # do not reuse `snap` (it is the node's output parquet)
                sql_p = cfg_no.get("sql")
                # Same reuse as the `publicado` source, for the same reason: the origin run's snapshot
                # is immutable, so a sibling that already read it resolved for all. No lineage here:
                # reading another pipeline's output does not record `leitura_lake`, and inventing an
                # event on the reuse path would create a trail the normal path does not have.
                if cadeia_id is not None and not forcar and not (sql_p and sql_p.strip()):
                    fp_cadeia = _fingerprint(no, [str(snap_origem)], pin or [], parametros)
                    chave_cadeia = (str(cadeia_id), fp_cadeia)
                    voo_extracao = await VOO_EXTRACAO.entrar(
                        chave_cadeia
                    )  # see the `publicado` branch
                    reuso = await _reuso_da_cadeia(
                        chave_cadeia,
                        execucao_id=execucao_id,
                        no_id=no_id,
                        snap=snap,
                        cadeia_id=cadeia_id,
                        fp=fp_cadeia,
                        marcar=_marcado,
                    )
                    if reuso is not None:
                        return reuso
                await (
                    _preparar_duck()
                )  # only now: a node that exits through reuse never opens the instance
                # VIEW for the same reason as the `publicado` origin: the snapshot is immutable.
                await _sql(
                    f"CREATE VIEW \"{alias}\" AS SELECT * FROM read_parquet('{snap_origem}')"
                )
                if sql_p and sql_p.strip():
                    await _sql(f'CREATE TABLE "{no_id}" AS {sql_p}')
                else:
                    # Pure passthrough, like the output: the node IS the origin's immutable snapshot.
                    # A VIEW instead of a materialized copy, and a snapshot by link instead of a new
                    # encoding; the hash is still computed over the view, the same number as its CTAS.
                    await _sql(f'CREATE VIEW "{no_id}" AS SELECT * FROM "{alias}"')
                    snap_repasse = Path(snap_origem)
                    arquivo_repasse = snap_origem
            else:
                cid = cfg_no.get("conexao_id")
                if not cid:
                    raise ErroAplicacao(
                        codigo="fonte_sem_conexao",
                        mensagem=f"Node '{no_id}' has no connection.",
                        status=400,
                    )
                # "Blank = all" ALSO at execution: a declared parameter without a value neutralizes
                # the predicate that compares it, instead of letting the literal `:nome` reach the
                # driver (error). The preview already did this, and it must match the execution.
                params_fonte = completar_declarados(definicao, parametros)
                sql_fonte = neutralizar_predicados_vazios(
                    cfg_no["sql"], {k for k, v in params_fonte.items() if v is None}
                )
                # PER-CHAIN EXTRACTION CACHE: within ONE chain (same as_of/scope, chained execution),
                # sibling pipelines asking for the SAME connection+SQL+parameters get the SAME data,
                # so re-extracting is pure I/O. Outside a chain (cadeia_id None) or across different
                # chains, `fonte` is ALWAYS fresh (as before).
                # `forcar` applies here too: re-running from a SOURCE is exactly the gesture of
                # someone who wants to re-read the origin, and serving the chain snapshot would
                # return the old data disguised as a re-read.
                if cadeia_id is not None and not forcar:
                    # Extraction identity uses only the parameters it depends on: two nodes with the
                    # SAME SQL and the SAME `:operacao_id` value, but where one pipeline does not
                    # declare `competencia`, used to get different fingerprints and never matched.
                    fp = _fingerprint(
                        no,
                        [],
                        pin or [],
                        _params_da_extracao(
                            no,
                            params_fonte,
                            parametros,
                            por_referencia=await conexoes.vincula_por_referencia(
                                uuid.UUID(str(cid))
                            ),
                        ),
                    )
                    # SINGLE FLIGHT: two pipelines in the chain asking for the SAME extraction at the
                    # same time would both extract, because the cache below only sees COMPLETED
                    # snapshots. The second waits for the first and exits through the cache. Released
                    # in the `finally`, AFTER the completion INSERT; if the first fails, the second
                    # finds nothing in the cache and extracts by itself.
                    voo_extracao = await VOO_EXTRACAO.entrar((str(cadeia_id), fp))
                    chave_cadeia = (str(cadeia_id), fp)
                    reuso = await _reuso_da_cadeia(
                        chave_cadeia,
                        execucao_id=execucao_id,
                        no_id=no_id,
                        snap=snap,
                        cadeia_id=cadeia_id,
                        fp=fp,
                        marcar=_marcado,
                    )
                    if reuso is not None:
                        return reuso
                # Read the COMPLETE SOURCE (limit None) WITHOUT an open metadata transaction:
                # `amostra` opens its own short one, only to resolve the credential. Reading millions
                # of rows from a SQL Server takes minutes, and a transaction held open that long
                # would pin `xmin` and block autovacuum for the whole database.
                # PARQUET goes DIRECTLY, bypassing `_carregar`. The generic path converts everything
                # to text (`serial` does `float(Decimal)` and `read_json` declares VARCHAR on every
                # column): a file `DECIMAL(6,2)` would reach the node SQL as `'1000.0'`. Another
                # pipeline's output and the lake dataset already come in typed, via `read_parquet`,
                # and comparing a typed side with an all-VARCHAR one makes the CAST written in the
                # SQL decide the cent. CSV/Excel deliberately keep VARCHAR: there is no type to
                # preserve, and it is what saves leading zeros in identifiers.
                # Credential resolved ONCE for the two uses below (direct parquet and extraction):
                # each `obter_acesso` is a transaction plus a vault decryption.
                acesso = await conexoes.obter_acesso(uuid.UUID(str(cid)))
                caminho_pq = conexoes.caminho_parquet_do_acesso(acesso)
                if caminho_pq:
                    await _preparar_duck()  # only now: the chain cache (above) does not need it
                    # SAME semantics as before, only typed: the file connector IGNORES the node SQL
                    # (`arquivo.amostra` reads the whole file), so the node stays the whole file.
                    # Applying the SQL here would change the behaviour of existing file sources.
                    await em_thread(
                        duck.execute,
                        f'CREATE TABLE "{no_id}" AS SELECT * FROM read_parquet(?)',
                        [caminho_pq],
                    )
                else:
                    # COMPLETE read through the connector's columnar path when it exists (SQL
                    # Server): data goes from the cursor to DuckDB in Arrow batches, without the
                    # list of dicts or the conversion on the event loop (see `conexoes/colunar.py`).
                    extracao = await conexoes.extrair(
                        uuid.UUID(str(cid)),
                        sql_fonte,
                        params_fonte,
                        acesso=acesso,
                        paralela=cfg_no.get("extracao_paralela"),
                    )
                    # The DuckDB slot only AFTER the extraction: it does not use the instance, and
                    # over a VPN it can take minutes. Taking it earlier would leave the
                    # `instancia.entrar` slots stuck waiting on the network, and, with the cap on
                    # simultaneous extractions, waiting their turn in the extraction queue.
                    await _preparar_duck()
                    await em_thread(_carregar_extracao, duck, no_id, extracao)
                    del (
                        extracao
                    )  # DuckDB already holds its copy; the Arrow data need not live any longer
        elif tipo == "sql":
            # `:nome` becomes `?` + a native bind (DuckDB does not understand `:nome`) and a
            # predicate against an empty parameter becomes `1=1`: same preparation as preview and source.
            params_sql = completar_declarados(definicao, parametros)
            sql_no, valores = preparar_duckdb(no["sql"], params_sql)
            # Open the instance and run the CTAS in a single hop to the thread (they used to be two).
            await _preparar_duck(
                lambda con: con.execute(f'CREATE TABLE "{no_id}" AS {sql_no}', valores)
            )
        elif tipo == "guiada":
            op_g = no["config"]["op"]
            validar_uniao(duck, op_g, preds)
            sqlg = gerar_sql(op_g, entradas=preds)
            # The guided operation's condition is free text, so it may reference `:nome` (sqlglot
            # normalizes it to `$nome`; `preparar_duckdb` recognizes both spellings).
            sqlg, valores_g = preparar_duckdb(sqlg, completar_declarados(definicao, parametros))
            try:
                await _sql(f'CREATE TABLE "{no_id}" AS {sqlg}', valores_g)
            except Exception as exc:  # noqa: BLE001 - DuckDB error sanitized for the user
                if op_g.get("modo") == "uniao":
                    raise ErroAplicacao(
                        codigo="uniao_incompativel",
                        mensagem=(
                            f"Union failed on node '{no_id}': the columns have incompatible "
                            "types across the inputs."
                        ),
                        status=400,
                    ) from exc
                raise
        elif tipo == "comparacao":
            if len(preds) < 2:
                raise ErroAplicacao(
                    codigo="comparacao_sem_entradas",
                    mensagem=f"Node '{no_id}' requires two inputs.",
                    status=400,
                )
            cfg_c = no.get("config") or {}
            esq = cfg_c.get("esquerda") or preds[0]
            dir_ = cfg_c.get("direita") or preds[1]
            sqlc = gerar_sql_comparacao(
                esq, dir_, cfg_c.get("chave"), incluir_iguais=bool(cfg_c.get("incluir_iguais"))
            )
            await _sql(f'CREATE TABLE "{no_id}" AS {sqlc}')
        elif tipo == "python":
            # Procedural code node (what SQL does not cover). Runs in an isolated SANDBOX:
            # predecessors go in as Parquet (name = node id), Parquet comes out and becomes the
            # node table. config: {codigo, linguagem?:"python"}. Deterministic by contract.
            cfg_py = no.get("config") or {}
            codigo = cfg_py.get("codigo")
            if not codigo or not str(codigo).strip():
                raise ErroAplicacao(
                    codigo="python_sem_codigo",
                    mensagem=f"Node '{no_id}' (code) has no config.codigo.",
                    status=400,
                )
            # ABSOLUTE paths: the sandbox runs with its own (temp) cwd, so relative ones would not resolve.
            entradas = {p: str((dir_exec / f"{p}.parquet").resolve()) for p in preds}
            saida_py = (dir_exec / f".{no_id}.py.parquet").resolve()
            # blocking `subprocess.run`: in a thread, otherwise a 10-minute sandbox freezes the loop
            await em_thread(executar_python, str(codigo), entradas, parametros or {}, saida_py)
            await _sql(f"CREATE TABLE \"{no_id}\" AS SELECT * FROM read_parquet('{saida_py}')")
        elif tipo == "saida":
            # Terminal node: passes the input data on and PUBLISHES to a stable destination (below).
            if not preds:
                raise ErroAplicacao(
                    codigo="saida_sem_entrada",
                    mensagem=f"Node '{no_id}' (output) has no input.",
                    status=400,
                )
            cfg_saida = no.get("config") or {}
            if any(
                isinstance(p, dict) and p.get("origem") == "coluna"
                for p in cfg_saida.get("particao") or []
            ):
                # COLUMN partition: publishing sorts by `rowid` (see `_publicar_saida`), which only
                # a table has.
                await _sql(f'CREATE TABLE "{no_id}" AS SELECT * FROM "{preds[0]}"')
            else:
                # Pure passthrough: the node IS the predecessor's (immutable) Parquet. A VIEW
                # instead of a materialized copy, and a snapshot by link (below) instead of a new
                # encoding: otherwise each output ran CTAS + COPY of the same data. The hash is
                # still computed here, over the view: same rows and types as the CTAS (both read
                # the same `read_parquet`), so the same number.
                await _sql(f'CREATE VIEW "{no_id}" AS SELECT * FROM "{preds[0]}"')
                snap_repasse = dir_exec / f"{preds[0]}.parquet"
        elif tipo == "escrita_sql":
            # EFFECT node: writes to an external database. OUTSIDE incremental reuse (always runs).
            reuso = await _reuso_efeito_concluido(execucao_id, no_id, snap)  # idempotency
            if reuso is not None:
                return reuso
            if len(preds) != 1:
                raise ErroAplicacao(
                    codigo="escrita_sem_entrada",
                    mensagem=f"Node '{no_id}' (write) requires exactly one input.",
                    status=400,
                )
            cfg_e = no.get("config") or {}
            conexao_id = uuid.UUID(str(cfg_e["conexao_id"]))
            cur_pred = duck.execute(f'SELECT * FROM "{preds[0]}"')
            colunas_pred = [d[0] for d in cur_pred.description]
            linhas_pred = [dict(zip(colunas_pred, row, strict=True)) for row in cur_pred.fetchall()]
            if not linhas_pred and not cfg_e.get("permitir_vazio", False):
                # Empty guard: 0 rows fails (critical so truncate_insert does not wipe the target).
                raise ErroAplicacao(
                    codigo="escrita_vazia",
                    mensagem=f"Node '{no_id}': 0 input rows and permitir_vazio=false.",
                    status=400,
                )
            # RE-CHECK of the flag + type + credential (defense in depth; the secret is resolved here).
            async with transacao() as conn:
                tipo_cx, config_cx, senha_cx = await conexoes.resolver_escrita(conn, conexao_id)
            plano = montar_plano(cfg_e, colunas_pred, tipo_cx)
            conector = get_conector_escrita(tipo_cx)
            # Data-idempotent only when the strategy converges (upsert/truncate_insert); append and
            # free SQL do not, so re-entry is refused instead of duplicating (shared fail-safe).
            idempotente = plano.modo == "tabela" and plano.estrategia in (
                "upsert",
                "truncate_insert",
            )
            await _marcar_efeito_ou_recusar(execucao_id, no_id, idempotente=idempotente)
            t_efeito = time.monotonic()
            resumo = await executar_escrita(
                conector,
                config_cx,
                senha_cx,
                plano,
                colunas_pred=colunas_pred,
                linhas_pred=linhas_pred,
                params=parametros,
            )
            dur_efeito = int((time.monotonic() - t_efeito) * 1000)
            criar_tabela_resumo(duck, no_id)
            duck.execute(
                f'INSERT INTO "{no_id}" VALUES (?, ?, ?, ?, ?, ?)',
                [
                    resumo["destino"],
                    resumo["estrategia"],
                    resumo["linhas_entrada"],
                    resumo["linhas_afetadas"],
                    "ok",
                    dur_efeito,
                ],
            )
            evento_efeito = {
                "no_id": no_id,
                "conexao_id": str(conexao_id),
                "destino": resumo["destino"],
                "estrategia": resumo["estrategia"],
                "linhas_entrada": resumo["linhas_entrada"],
                "linhas_afetadas": resumo["linhas_afetadas"],
            }
        elif tipo == "http":
            # EFFECT node: calls an external API; the response becomes the node's data. OUTSIDE
            # incremental reuse. http is treated as NOT idempotent (a POST may create; the method
            # does not guarantee it), so re-entry after the mark is REFUSED (conservative fail-safe).
            reuso = await _reuso_efeito_concluido(execucao_id, no_id, snap)
            if reuso is not None:
                return reuso
            cfg_h = no.get("config") or {}
            modo = cfg_h.get("modo", "unico")
            conexao_id = uuid.UUID(str(cfg_h["conexao_id"]))
            if modo in ("por_linha", "lote") and len(preds) != 1:
                raise ErroAplicacao(
                    codigo="http_sem_entrada",
                    mensagem=f"Node '{no_id}' (http {modo}) requires exactly one input.",
                    status=400,
                )
            if modo == "unico" and len(preds) > 1:
                raise ErroAplicacao(
                    codigo="http_entradas",
                    mensagem=f"Node '{no_id}' (single http) accepts at most one input.",
                    status=400,
                )
            colunas_pred, linhas_pred = [], []
            if preds:
                cur_pred = duck.execute(f'SELECT * FROM "{preds[0]}"')
                colunas_pred = [d[0] for d in cur_pred.description]
                linhas_pred = [
                    dict(zip(colunas_pred, row, strict=True)) for row in cur_pred.fetchall()
                ]
            # RE-CHECK of the flag + api_rest type + credential (secret resolved here, at runtime).
            async with transacao() as conn:
                config_cx, senha_cx = await conexoes.resolver_http(conn, conexao_id)
            plano = montar_requisicoes(
                cfg_h, colunas_pred, linhas_pred, parametros, config_cx["url"]
            )
            await _marcar_efeito_ou_recusar(execucao_id, no_id, idempotente=False)
            tol = cfg_h.get("tolerancia_falha", "abortar")
            if not plano.requisicoes:
                # 0 rows -> 0 requests (por_linha/lote): EMPTY snapshot with the mode's schema.
                _carregar(
                    duck,
                    no_id,
                    [],
                    colunas=colunas_snapshot(
                        modo, colunas_pred, plano.resposta_campos, plano.incluir_resposta_completa
                    ),
                )
                evento_efeito = {
                    "no_id": no_id,
                    "conexao_id": str(conexao_id),
                    "url": plano.url_destino,
                    "metodo": plano.metodo,
                    "requisicoes": 0,
                    "falhas": 0,
                }
            else:
                cfg_g = get_settings()
                t_efeito = time.monotonic()
                resultados, disparados = await executar_http(
                    config_cx,
                    senha_cx,
                    plano,
                    timeout=int(cfg_h.get("timeout_segundos") or cfg_g.http_no_timeout_segundos),
                    concorrencia_max=cfg_g.http_no_concorrencia_max,
                    tolerancia=tol,
                )
                dur_efeito = int((time.monotonic() - t_efeito) * 1000)
                falhas = [i for i, r in enumerate(resultados) if r is None or r.get("erro")]
                if tol == "abortar" and any(r and r.get("erro") for r in resultados):
                    # The 1st failure aborted the pending ones: the node fails and reports the row,
                    # the cause and how many requests were ALREADY fired (explicit partial effect,
                    # not silent).
                    i0 = next(i for i, r in enumerate(resultados) if r and r.get("erro"))
                    raise ErroAplicacao(
                        codigo="http_falha",
                        mensagem=(
                            f"Node '{no_id}': request #{i0} failed ({resultados[i0]['erro']}). "
                            f"{disparados} request(s) already fired; the effect may be "
                            "partial."
                        ),
                        status=400,
                    )
                executados = [r for r in resultados if r is not None]
                # tolerancia='continuar': by default the node fails if 100% of the requests failed
                # (safety net against misconfiguration). `permitir_falha_total=true` turns that net
                # off: the node COMPLETES even with 100% failures, loading the failures' snapshot
                # for downstream. Use case: idempotent invoice issuing, where the ledger node MUST
                # run to record the deterministic failures (e.g. "does not exist in orders") and
                # exclude them from the next rounds; aborting would prevent that.
                if (
                    not cfg_h.get("permitir_falha_total", False)
                    and executados
                    and all(r.get("erro") for r in executados)
                ):
                    raise ErroAplicacao(
                        codigo="http_falha_total",
                        mensagem=f"Node '{no_id}': all {len(executados)} requests failed.",
                        status=400,
                    )
                if modo == "unico":
                    r0 = resultados[0]
                    cols, linhas_snap = achatar_unico(
                        r0["resposta"] or "", r0["status"], plano.resposta_json_path
                    )
                    _carregar(duck, no_id, linhas_snap, colunas=cols)
                else:
                    linhas_snap = [
                        linha_snapshot(
                            modo,
                            plano.requisicoes[i],
                            resultados[i],
                            colunas_pred,
                            plano.resposta_campos,
                            plano.incluir_resposta_completa,
                        )
                        for i in range(len(plano.requisicoes))
                        if resultados[i] is not None
                    ]
                    _carregar(
                        duck,
                        no_id,
                        linhas_snap,
                        colunas=colunas_snapshot(
                            modo,
                            colunas_pred,
                            plano.resposta_campos,
                            plano.incluir_resposta_completa,
                        ),
                    )
                evento_efeito = {
                    "no_id": no_id,
                    "conexao_id": str(conexao_id),
                    "url": plano.url_destino,
                    "metodo": plano.metodo,
                    "requisicoes": disparados,
                    "falhas": len(falhas),
                }
        elif tipo == "email":
            # EFFECT node: sends e-mail. OUTSIDE incremental reuse and NOT idempotent: there is no
            # "resend without duplicating", so re-entry after the mark is REFUSED.
            reuso = await _reuso_efeito_concluido(execucao_id, no_id, snap)
            if reuso is not None:
                return reuso
            cfg_m = no.get("config") or {}
            conexao_id = uuid.UUID(str(cfg_m["conexao_id"]))
            if len(preds) > 1:
                raise ErroAplicacao(
                    codigo="email_entradas",
                    mensagem=f"Node '{no_id}' (email) accepts at most one input.",
                    status=400,
                )
            # RE-CHECK of the smtp type + flag + credential (secret resolved here, at runtime).
            async with transacao() as conn:
                config_cx, senha_cx = await conexoes.resolver_email(conn, conexao_id)
            plano = montar_plano_email(
                cfg_m,
                dados=DadosDuckDB(duck),
                entrada=preds[0] if preds else None,
                params=parametros,
                config_conexao=config_cx,
            )
            await _marcar_efeito_ou_recusar(execucao_id, no_id, idempotente=False)
            resumo_linhas, enviados = await executar_email(
                get_conector_email("smtp"),
                config_cx,
                senha_cx,
                plano,
                tolerancia=cfg_m.get("tolerancia_falha", "abortar"),
                timeout=cfg_m.get("timeout_segundos"),
                tentativas=cfg_m.get("tentativas"),
            )
            criar_tabela_resumo_email(duck, no_id)
            for linha_r in resumo_linhas:
                duck.execute(f'INSERT INTO "{no_id}" VALUES (?, ?, ?, ?, ?, ?, ?)', linha_r)
            falhas = sum(1 for linha_r in resumo_linhas if linha_r[4] != "ok")
            # Trail WITHOUT body and WITHOUT attachment (the content may hold business data and is
            # large): who sent, to whom, with what subject and how many attachments; enough to audit.
            evento_efeito = {
                "no_id": no_id,
                "conexao_id": str(conexao_id),
                "canal": "email",
                "servidor": plano.host_destino,
                "remetente": plano.remetente,
                "destinatarios": [m.destinatarios for m in plano.mensagens],
                "assuntos": sorted({m.assunto for m in plano.mensagens}),
                "anexos": sum(len(m.anexos) for m in plano.mensagens),
                "enviados": enviados,
                "falhas": falhas,
            }
        elif tipo == "ingestao":
            # EFFECT node: downloads from the public origin and writes to the lake (landing ->
            # bronze). OUTSIDE incremental reuse: the point of ingestion is to fetch what changed
            # out there, and a node reusing a snapshot by fingerprint would never bring a new slice.
            #
            # IDEMPOTENT, unlike e-mail: the `(fonte, chave_lote, sha256)` uniqueness in the
            # database turns a repeated slice with the same bytes into a no-op. That is why
            # re-entry is allowed (`idempotente=True`) instead of refused: refusing would trap the
            # slice in a limbo only an admin could leave, to avoid a duplication that cannot happen.
            reuso = await _reuso_efeito_concluido(execucao_id, no_id, snap)
            if reuso is not None:
                return reuso
            if preds:
                raise ErroAplicacao(
                    codigo="ingestao_com_entrada",
                    mensagem=(
                        f"Node '{no_id}' (ingestion) accepts no input: it fetches from the external "
                        "origin. To chain pipelines, use a source node with origin 'pipeline'."
                    ),
                    status=400,
                )
            from ..ingestao import service as ingestao_service

            cfg_i = no.get("config") or {}
            fonte_id_i = no_ingestao.fonte_id_de(cfg_i, no_id)
            inicio_i, fim_i = no_ingestao.janela_de(cfg_i)
            await _marcar_efeito_ou_recusar(execucao_id, no_id, idempotente=True)
            resultado_i = await ingestao_service.executar_no_pipeline(
                fonte_id=fonte_id_i, inicio=inicio_i, fim=fim_i
            )
            no_ingestao.criar_tabela_log(duck, no_id)
            no_ingestao.inserir_log(duck, no_id, resultado_i["fatias"])
            # The effect node's trail keeps WHAT was ingested and the outcome, never a URL with
            # credentials (the origin is public, but the rule holds anyway) or payload content.
            evento_efeito = {
                "no_id": no_id,
                "canal": "ingestao",
                "fonte_id": fonte_id_i,
                "dataset": resultado_i["dataset"],
                "ingestao_execucao_id": resultado_i["execucao_id"],
                "janela": resultado_i["janela"],
                "totais": resultado_i["totais"],
            }
            # A quarantined slice does not fail the node (DATA failure != INFRA failure), but it
            # would vanish if the node just said "completed": the log stays in the snapshot and
            # the reason in the trail.
            if resultado_i["totais"].get("quarentena"):
                evento_efeito["quarentena"] = resultado_i["totais"]["quarentena"]
        else:
            raise ErroAplicacao(
                codigo="tipo_no", mensagem=f"Unsupported node type '{tipo}'.", status=400
            )

        # Snapshot + CONTENT hash (enables reuse) in a single hop and, except for the output
        # (which still publishes from the instance), the instance's end in the same hop. The
        # thread becomes the OWNER of the instance (`duck = None` before the await): cancelling
        # this await does not open a second thread closing the same connection in the `finally`.
        con_duck, devolver = duck, None
        if tipo != "saida":  # the output still publishes from the instance
            devolver, slot, duck = slot, None, None
        # Pure passthrough: if another node of this process already read THIS file with this same
        # view, the per-row `to_json` was already paid for.
        pronto = _hash_do_arquivo(arquivo_repasse) if arquivo_repasse else None
        h = await em_thread(_snapshot_e_hash, con_duck, no_id, snap, snap_repasse, devolver, pronto)
        if arquivo_repasse and pronto is None:
            _lembrar_hash_do_arquivo(arquivo_repasse, h)
        linhas_n = int(h.split(":", 1)[0])

        # Publish the dataset (empty guard inside) BEFORE marking the node completed: the output
        # fails if empty, preserving the earlier published one. Always re-publishes (outside reuse).
        async def _liberar_duck() -> None:
            nonlocal slot, duck
            if slot is not None:
                devolver, slot, duck = slot, None, None
                await em_thread(devolver.fechar)

        publicado = (
            await _publicar_saida(
                duck,
                no,
                no_id,
                linhas_n,
                execucao_id=execucao_id,
                sem_publicar=sem_publicar,
                # REDACTED: the parameters go into the manifest and the path, which go to the bucket,
                # where nothing is deleted afterwards. The real value of a `segredo: true` only
                # exists in memory, on the request path that needs it.
                parametros=redigir_segredos(definicao, parametros),
                liberar_duck=_liberar_duck,
                # The manifest normalizes by the DECLARED TYPE (`as_of`/`data` become the day),
                # never by a regex over the text, which truncated the time of any plain `texto`.
                tipos_parametro={
                    p["nome"]: p.get("tipo")
                    for p in parametros_declarados(definicao)
                    if isinstance(p, dict) and p.get("nome")
                },
            )
            if tipo == "saida"
            else None
        )
        if slot is not None:
            # Output: the instance only served the publication. Return the slot before completion;
            # its memory does not wait for the database round trip.
            devolver, slot, duck = slot, None, None
            await em_thread(devolver.fechar)
        dur = int((time.monotonic() - t0) * 1000)
        await _marcado()
        # With a trail (effect, lake read): completion + audit in the SAME transaction.
        # Without a trail it is a single statement: autocommit, skipping the BEGIN/COMMIT round trips.
        com_trilha = evento_efeito is not None or evento_leitura is not None
        async with transacao() if com_trilha else autocommit() as conn:
            await conn.execute(
                text(
                    "INSERT INTO execucao_nos (execucao_id, no_id, status, linhas, hash, snapshot, duracao_ms, fingerprint, reaproveitado, concluido_em) "
                    "VALUES (:e, :n, 'concluida', :l, :h, :s, :d, :fp, false, now()) "
                    "ON CONFLICT (execucao_id, no_id) DO UPDATE SET "
                    "status='concluida', linhas=:l, hash=:h, snapshot=:s, duracao_ms=:d, "
                    "fingerprint=:fp, reaproveitado=false, erro=NULL, concluido_em=now()"
                ),
                {
                    "e": execucao_id,
                    "n": no_id,
                    "l": linhas_n,
                    "h": h,
                    "s": str(snap),
                    "d": dur,
                    "fp": fp,
                },
            )
            # EFFECT node: the `escrita_externa` trail is written in the SAME transaction as the
            # 'concluida' upsert (atomicity). No secret/payload in the diff; only destination/counts.
            if evento_efeito is not None:
                await registrar(
                    conn,
                    acao=Acao.EXECUTAR,
                    recurso_tipo="escrita_externa",
                    recurso_id=execucao_id,
                    ator_id=iniciada_por,
                    antes=None,
                    depois=evento_efeito,
                )
            # Lake source: WHICH bronze batches fed this node. In the same transaction, for the
            # same reason, and because redoing the resolution later would return another set (new
            # batches arrive), making the lineage impossible to rebuild.
            if evento_leitura is not None:
                await registrar(
                    conn,
                    acao=Acao.EXECUTAR,
                    recurso_tipo="leitura_lake",
                    recurso_id=execucao_id,
                    ator_id=iniciada_por,
                    antes=None,
                    depois=evento_leitura,
                )
        # After the commit: memory only says what the database already says.
        _lembrar_no(execucao_id, no_id, h, snap)
        if chave_cadeia is not None:
            _lembrar_da_cadeia(chave_cadeia, str(snap), h, linhas_n)
        return {
            "no_id": no_id,
            "linhas": linhas_n,
            "hash": h,
            "snapshot": str(snap),
            "duracao_ms": dur,
            "reaproveitado": False,
            "publicado_em": publicado,
        }
    except Exception as exc:  # noqa: BLE001 - records the node as failed and propagates
        # The NODE error keeps the REAL detail (log for the specialist to debug in Studio/Runs);
        # the API envelope stays generic (no stack trace).
        detalhe = (
            exc.mensagem if isinstance(exc, ErroAplicacao) else f"{type(exc).__name__}: {exc}"
        )[:2000]
        msg_envelope = (
            exc.mensagem
            if isinstance(exc, ErroAplicacao)
            else "Node execution failed (SQL or source)."
        )
        # The failure is the last word on the node: the start mark must not land after it.
        with contextlib.suppress(Exception):
            await _marcado()
        async with autocommit() as conn:  # one statement (the failure upsert)
            await conn.execute(
                text(
                    "INSERT INTO execucao_nos (execucao_id, no_id, status, erro, concluido_em) "
                    "VALUES (:e, :n, 'falha', :err, now()) "
                    "ON CONFLICT (execucao_id, no_id) DO UPDATE SET status='falha', erro=:err"
                ),
                {"e": execucao_id, "n": no_id, "err": detalhe},
            )
        raise ErroAplicacao(codigo="erro_execucao", mensagem=msg_envelope, status=400) from exc
    finally:
        if marca is not None:  # cancelled before being awaited: no orphan write left in flight
            marca.cancel()
        if slot is not None:
            # Returning the slot (dropping the schema, destroying the own instance) is native
            # work: off the loop.
            await em_thread(slot.fechar)
        if voo_extracao is not None:
            VOO_EXTRACAO.sair(voo_extracao)


async def _execucao_da_pagina(conn: AsyncConnection, pagina_id: uuid.UUID) -> dict | None:
    """`execucao` config (operator + derivations) of the PUBLISHED page, used to resolve the
    page derivations at trigger time (scheduling). None if the page has no published version."""
    row = (
        await conn.execute(
            text(
                "SELECT pv.definicao FROM paginas p JOIN pagina_versoes pv ON pv.id = p.versao_publicada_id "
                "WHERE p.id = :id"
            ),
            {"id": pagina_id},
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    d = json.loads(row) if isinstance(row, str) else row
    return d.get("execucao") if isinstance(d, dict) else None


# --- Duplicate run: same pipeline + same parameters already in progress ------------------------
# The key is computed IN SQL, always by the same expression (migration 0038): `jsonb::text` is
# canonical in Postgres, so key order/whitespace in the dict do not change the key. `:p_txt` is
# the id as TEXT (its own parameter: the same `:p` bound as uuid cannot be reused in a CAST).
SQL_CHAVE_CONCORRENCIA = "md5(:p_txt || CAST(:params AS jsonb)::text)"
NOME_UNIQUE_EM_VOO = "ux_execucoes_em_voo"


async def _preparar_execucao(
    pipeline_id: uuid.UUID,
    versao_numero: int | None,
    parametros: dict | None,
    pagina_id: uuid.UUID | None,
) -> dict:
    """Head of `criar_execucao` with no side effect: loads the definition, resolves derived
    values and normalizes/redacts the parameters. Split out so the chain pre-check
    (`executar_encadeado`) asks the SAME question as the guard, without duplicating normalization."""
    async with transacao() as conn:
        det = await ps.obter(conn, pipeline_id, versao_numero)
    # SINGLE choke point of every run (UI, cron scheduler, re-run, chained run): this is where,
    # and only where, "pipeline disabled" must block, so it holds for every trigger without
    # duplicating the check in each route that starts a run. Before any INSERT/effect.
    if not det["ativo"]:
        raise ErroAplicacao(
            codigo="pipeline_desativado",
            mensagem="Pipeline is disabled; re-enable it before running.",
            status=409,
        )
    if det["numero"] == 0:
        raise ErroAplicacao(
            codigo="sem_definicao", mensagem="Pipeline has no definition to run.", status=400
        )
    definicao = det["definicao"]
    # Resolve the DERIVED parameters (e.g. `sistema` from `operacao_id` via a map) in the backend,
    # so a run works from ANY surface (Studio and Portal). Respects what already came resolved.
    # Without this, running from Studio leaked the literal `:derivado` to the source.
    # No transaction around it: resolving a derived value READS A SOURCE (the map may be an
    # entire table), and a transaction left open during that read pins the metadata `xmin`.
    parametros = await resolver_derivados(definicao, parametros)
    # PAGE derivations (execucao.derivacoes, e.g. the Check's `sistema`): resolved in the backend
    # so page scheduling works without the client. Respects what the Portal already sent.
    if pagina_id:
        async with transacao() as conn:  # only the page config (metadata)
            execucao_cfg = await _execucao_da_pagina(conn, pagina_id)
        parametros = await resolver_derivacoes_pagina(execucao_cfg, parametros)
    params = normalizar_parametros(
        definicao, parametros
    )  # validates + applies defaults (400 if invalid)
    params_persist = redigir_segredos(definicao, params)  # what goes to the database and the trail
    return {
        "definicao": definicao,
        "versao": det["numero"],
        "params": params,
        "params_persist": params_persist,
    }


async def parametros_como_guardados(
    pipeline_id: uuid.UUID, parametros: dict | None, *, versao_numero: int | None = None
) -> dict:
    """The parameters as the ROOT run would store them, creating nothing.

    It is the SAME head as the trigger (`_preparar_execucao`: derived values, normalization,
    redaction), over the SAME slice the chain applies at the root
    (`filtrar_parametros_declarados`). It exists for callers that need to recognize a run by its
    parameters without reimplementing normalization: an external adapter that mirrored it would
    break on every new rule here (a default, a calendar-derived value), producing redeliveries
    that did not match and a second run of the same period.

    `fora_da_raiz`: names the root does not declare. In a chain the trigger accepts them (an
    upstream pipeline declares them) and they VANISH from the root's run, so whoever recognizes by
    the root does not see them.
    `relativos`: values that depend on the day (`atual`, `hoje`...): the same input normalizes
    differently tomorrow, so a run cannot be recognized by them."""
    async with transacao() as conn:
        det = await ps.obter(conn, pipeline_id, versao_numero)
    definicao = det["definicao"]
    declarados = {
        p["nome"]
        for p in parametros_declarados(definicao)
        if isinstance(p, dict) and isinstance(p.get("nome"), str)
    }
    recorte = filtrar_parametros_declarados(definicao, parametros)
    prep = await _preparar_execucao(pipeline_id, versao_numero, recorte, None)
    return {
        "parametros": prep["params_persist"],
        "versao": prep["versao"],
        "fora_da_raiz": sorted(k for k in (parametros or {}) if k not in declarados),
        "relativos": parametros_relativos(definicao, recorte),
    }


async def execucao_em_voo(pipeline_id: uuid.UUID, params_persist: dict) -> dict | None:
    """The `executando` run of this pipeline with the SAME key, or None.

    Always asks Temporal, not only after the reaper's minimum age: an `executando` row whose
    workflow died (worker restarted before `finalizar`) is common in dev, and letting it block the
    next trigger for 30 minutes kills the "failed, fixed, run again" flow. A workflow that is not
    running is closed right here, with the same verdict as the reaper, and does not block.

    Temporal not answering (network) or inline execution (`usar_temporal=false`): there is no
    verdict, and without a verdict the row COUNTS, which is the safe state; the worker's reaper
    closes it later.
    """
    async with transacao() as conn:
        linha = (
            await conn.execute(
                text(
                    "SELECT id, iniciada_em FROM execucoes WHERE status = 'executando' "
                    f"AND chave_concorrencia = {SQL_CHAVE_CONCORRENCIA}"
                ),
                {"p_txt": str(pipeline_id), "params": json.dumps(params_persist)},
            )
        ).first()
    if linha is None:
        return None
    em_voo = {"id": linha[0], "iniciada_em": linha[1]}
    cfg = get_settings()
    if not cfg.usar_temporal:
        return em_voo
    try:
        cliente = await reconciliacao.conectar(cfg.temporal_host)
        veredito = await reconciliacao.consultar_uma(cliente, linha[0])
    except Exception:  # noqa: BLE001 - the orchestrator's silence is not a verdict (see reconciliacao)
        return em_voo
    if veredito.viva:
        return em_voo
    await reconciliacao.fechar_orfa(linha[0], veredito.detalhe or reconciliacao.MENSAGEM_ORFA)
    return None


def _erro_em_andamento(em_voo: dict) -> ErroAplicacao:
    return ErroAplicacao(
        codigo="execucao_em_andamento",
        mensagem="A run of this pipeline with the same parameters is already in progress.",
        status=409,
        detalhes=[DetalheErro(campo="execucao_id", mensagem=str(em_voo["id"]))],
    )


async def _recusar_duplicata(
    *,
    pipeline_id: uuid.UUID,
    em_voo: dict,
    params_persist: dict,
    origem: str,
    ator_id: uuid.UUID | None,
    ip=None,
    request_id=None,
) -> ErroAplicacao:
    """Audits the rejected trigger (own transaction, since the INSERT one does not exist) and returns
    the 409. Records who tried, what, and against which in-flight run; without it a scheduler that
    deferred repeatedly would leave no trace of why."""
    async with transacao() as conn:
        await registrar(
            conn,
            acao=Acao.EXECUTAR,
            recurso_tipo="execucao",
            recurso_id=em_voo["id"],
            ator_id=ator_id,
            antes=None,
            depois={
                "recusado": "execucao_em_andamento",
                "pipeline_id": str(pipeline_id),
                "parametros": params_persist,
                "origem": origem,
            },
            request_id=request_id,
            ip=ip,
        )
    return _erro_em_andamento(em_voo)


async def verificar_raiz_disponivel(
    *,
    pipeline_id: uuid.UUID,
    versao_numero: int | None,
    parametros: dict | None,
    pagina_id: uuid.UUID | None,
    origem: str,
    ator_id: uuid.UUID | None,
    ip=None,
    request_id=None,
) -> None:
    """Pre-check of a chain's ROOT, before creating/running any dependency. Without it the chain
    would create (or run, inline) the upstreams and only then the root would get a 409, leaving
    `executando` runs without a workflow for the reaper to close."""
    prep = await _preparar_execucao(pipeline_id, versao_numero, parametros, pagina_id)
    em_voo = await execucao_em_voo(pipeline_id, prep["params_persist"])
    if em_voo is not None:
        raise await _recusar_duplicata(
            pipeline_id=pipeline_id,
            em_voo=em_voo,
            params_persist=prep["params_persist"],
            origem=origem,
            ator_id=ator_id,
            ip=ip,
            request_id=request_id,
        )


async def criar_execucao(
    *,
    pipeline_id: uuid.UUID,
    ator_id: uuid.UUID,
    parametros: dict | None = None,
    pagina_id: uuid.UUID | None = None,
    versao_numero: int | None = None,
    origem: str = "manual",
    agendamento_id: uuid.UUID | None = None,
    cadeia_id: uuid.UUID | str | None = None,
    cadeia_raiz: bool = False,
    reexecucao_de: uuid.UUID | str | None = None,
    reexecucao_desde_no: str | None = None,
    ip=None,
    request_id=None,
) -> dict:
    """Loads the definition to run, validates runtime parameters against the declared ones, creates
    the run (status executando) and audits it (with the parameters in the diff). `versao_numero`
    runs a SPECIFIC version (e.g. the one selected in the dropdown) without changing it or creating
    another; without it, the latest. `pagina_id` records which page the operator triggered from
    (null = Studio). `origem` distinguishes 'manual' from 'agendado' (with `agendamento_id` pointing
    at the schedule). `cadeia_id` (pipeline DAG) marks runs of the SAME chained-run trigger and
    enables `fonte` reuse across sibling pipelines of the chain (same as_of); None = solo run,
    always-fresh source. `cadeia_raiz` marks WHICH run of the chain was the requested one (the
    others are dependencies that ran as required tables); the listing uses it as the group header.
    `reexecucao_de` and `reexecucao_desde_no` (origem 'reexecucao') link this run to the original
    and say from which point it started.
    Returns {execucao_id, versao, definicao, ordem, parametros}."""
    prep = await _preparar_execucao(pipeline_id, versao_numero, parametros, pagina_id)
    definicao, versao = prep["definicao"], prep["versao"]
    params, params_persist = prep["params"], prep["params_persist"]
    # Duplicate-run guard: only the trigger's ROOT gets a key. A dependency inside a chain gets none
    # (NULL does not collide), because two chains needing the same upstream is routine, and
    # blocking the upstream would abort the whole chain.
    com_chave = cadeia_id is None or cadeia_raiz
    if com_chave:
        em_voo = await execucao_em_voo(pipeline_id, params_persist)
        if em_voo is not None:
            raise await _recusar_duplicata(
                pipeline_id=pipeline_id,
                em_voo=em_voo,
                params_persist=params_persist,
                origem=origem,
                ator_id=ator_id,
                ip=ip,
                request_id=request_id,
            )
    exec_id = uuid.uuid4()
    # Pin the PUBLISHED versions of the libraries the pipeline references. The run uses the exact
    # version pinned here (reproducibility); it goes into the stamped report's trace.
    bib_ids = [uuid.UUID(b) for b in bibliotecas_usadas(definicao)]
    async with transacao() as conn:
        pin = await bf_service.resolver_pin(conn, bib_ids) if bib_ids else []
    try:
        await _inserir_execucao(
            exec_id=exec_id,
            pipeline_id=pipeline_id,
            versao=versao,
            ator_id=ator_id,
            pagina_id=pagina_id,
            params_persist=params_persist,
            pin=pin,
            origem=origem,
            agendamento_id=agendamento_id,
            cadeia_id=cadeia_id,
            cadeia_raiz=cadeia_raiz,
            reexecucao_de=reexecucao_de,
            reexecucao_desde_no=reexecucao_desde_no,
            com_chave=com_chave,
            ip=ip,
            request_id=request_id,
        )
    except IntegrityError as e:
        # Two triggers both passed the pre-check and the partial index decided: the loser of the
        # race gets the SAME 409, pointing at the winner. The transaction (INSERT + audit trail)
        # was already rolled back, so no record is left of a trigger that did not happen.
        if NOME_UNIQUE_EM_VOO not in str(getattr(e, "orig", e)):
            raise
        em_voo = await execucao_em_voo(pipeline_id, params_persist)
        if em_voo is None:  # the winner finished between the race and now: the slot is free
            raise ErroAplicacao(
                codigo="execucao_em_andamento",
                mensagem="An identical trigger was just accepted. Try again.",
                status=409,
            ) from e
        raise await _recusar_duplicata(
            pipeline_id=pipeline_id,
            em_voo=em_voo,
            params_persist=params_persist,
            origem=origem,
            ator_id=ator_id,
            ip=ip,
            request_id=request_id,
        ) from e
    return {
        "execucao_id": str(exec_id),
        "versao": versao,
        "definicao": definicao,
        "ordem": ordem_topologica(definicao),
        "parametros": params,
        "bibliotecas": pin,
    }


async def _inserir_execucao(
    *,
    exec_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    versao: int,
    ator_id,
    pagina_id,
    params_persist: dict,
    pin: list,
    origem: str,
    agendamento_id,
    cadeia_id,
    cadeia_raiz: bool,
    reexecucao_de,
    reexecucao_desde_no,
    com_chave: bool,
    ip,
    request_id,
) -> None:
    """INSERT into `execucoes` + audit trail, in the SAME transaction. `chave_concorrencia` uses the
    SAME expression as the guard (`SQL_CHAVE_CONCORRENCIA`); NULL when the run is a chain dependency."""
    async with transacao() as conn:
        await conn.execute(
            text(
                "INSERT INTO execucoes (id, pipeline_id, versao_numero, status, iniciada_por, pagina_id, parametros, bibliotecas_versoes, origem, agendamento_id, cadeia_id, cadeia_raiz, reexecucao_de, reexecucao_desde_no, chave_concorrencia) "
                "VALUES (:id, :p, :v, 'executando', :a, :pag, CAST(:params AS jsonb), CAST(:bibvers AS jsonb), :origem, :agid, :cadeia, :raiz, :reexec, :desde, "
                f"CASE WHEN :com_chave THEN {SQL_CHAVE_CONCORRENCIA} ELSE NULL END)"
            ),
            {
                "id": exec_id,
                "p": pipeline_id,
                "p_txt": str(pipeline_id),
                "com_chave": com_chave,
                "v": versao,
                "a": ator_id,
                "pag": pagina_id,
                # REDACTED: a `segredo: true` parameter is never persisted or audited in clear text
                # (the trail is append-only and permanent). The real value stays in memory only, in
                # the `parametros` returned to whoever executes the nodes.
                "params": json.dumps(params_persist),
                "bibvers": json.dumps(pin),
                "origem": origem,
                "agid": agendamento_id,
                "cadeia": cadeia_id,
                "raiz": cadeia_raiz,
                "reexec": reexecucao_de,
                "desde": reexecucao_desde_no,
            },
        )
        await registrar(
            conn,
            acao=Acao.EXECUTAR,
            recurso_tipo="execucao",
            recurso_id=exec_id,
            ator_id=ator_id,
            antes=None,
            depois={
                "pipeline_id": str(pipeline_id),
                "versao": versao,
                "parametros": params_persist,  # no secret in the trail (append-only, permanent)
                "pagina_id": str(pagina_id) if pagina_id else None,
                "bibliotecas": pin,
                "origem": origem,
                "agendamento_id": str(agendamento_id) if agendamento_id else None,
                "cadeia_id": str(cadeia_id) if cadeia_id else None,
                "cadeia_raiz": cadeia_raiz,
                "reexecucao_de": str(reexecucao_de) if reexecucao_de else None,
                "reexecucao_desde_no": reexecucao_desde_no,
            },
            request_id=request_id,
            ip=ip,
        )


async def finalizar(
    *,
    execucao_id: uuid.UUID,
    erro: str | None = None,
    somente_se_executando: bool = False,
    sem_publicar: bool = False,
) -> None:
    """Closes the run. `somente_se_executando` makes the write CONDITIONAL on the current status.

    The UPDATE is unconditional by default because on the normal path the run's owner finalizes it,
    exactly once. But chain cleanup (`ExecutarDAG`) sweeps PRE-CREATED runs that will never run, and
    a blind UPDATE there would rewrite as `falha` a sibling that had already COMPLETED, replacing a
    good snapshot with a misleading outcome on screen and in the trail. With the guard, anything
    that already finished (in any way) stays as is and only what is stuck in `executando` is closed.

    This is also what lets cleanup be broad instead of depending on a list of what "already
    started": a run whose child workflow failed to START is in no list and would stay `executando`
    forever, with the panel showing a run in progress that nobody is executing.

    `sem_publicar` (root upstream with `montante_publica: false`) turns off eager report
    generation: the artifact is also an object in the lake, and a run that does not publish
    publishes nothing."""
    status = "falha" if erro else "concluida"
    sql = "UPDATE execucoes SET status=:st, erro=:e, concluida_em=now() WHERE id=:id"
    if somente_se_executando:
        sql += " AND status = 'executando'"
    async with transacao() as conn:
        await conn.execute(text(sql), {"id": execucao_id, "st": status, "e": erro})
    if status == "concluida" and not sem_publicar:
        # EAGER report generation: every default layout published for a node of this run becomes an
        # artifact now, smallest snapshot first, off the critical path (the run is already closed
        # above). A failure here is a log entry, never an exception: a layout that does not match
        # must not turn a completed run into a failure.
        import logging

        from ..relatorios import service as relatorios

        try:
            await relatorios.enfileirar_padroes(execucao_id)
        except Exception:  # noqa: BLE001 - the run already completed; the report is additive
            logging.getLogger("cflow_core.execucao").exception(
                "relatorios_padrao_erro", extra={"extra_campos": {"execucao_id": str(execucao_id)}}
            )


async def executar(
    *,
    pipeline_id: uuid.UUID,
    versao_numero: int | None = None,
    ator_id: uuid.UUID,
    parametros: dict | None = None,
    pagina_id: uuid.UUID | None = None,
    origem: str = "manual",
    agendamento_id: uuid.UUID | None = None,
    fontes_execucao: dict | None = None,
    cadeia_id: uuid.UUID | str | None = None,
    cadeia_raiz: bool = False,
    ip: str | None = None,
    request_id: str | None = None,
    sem_publicar: bool = False,
) -> dict:
    """INLINE run (no Temporal): orchestrates the nodes in topological order by calling executar_no.
    The Temporal path (worker) uses the SAME functions (criar_execucao/executar_no/finalizar).
    `fontes_execucao` (chaining) is passed to each node (pipeline-source freshness). `cadeia_id`
    (pipeline DAG) enables `fonte` reuse across sibling pipelines of the same trigger.
    `sem_publicar`: root upstream with `montante_publica: false`, the same signal as
    `ExecutarPipeline`, all the way to `executar_no` and `finalizar`."""
    ctx = await criar_execucao(
        pipeline_id=pipeline_id,
        ator_id=ator_id,
        parametros=parametros,
        pagina_id=pagina_id,
        versao_numero=versao_numero,
        origem=origem,
        agendamento_id=agendamento_id,
        cadeia_id=cadeia_id,
        cadeia_raiz=cadeia_raiz,
        ip=ip,
        request_id=request_id,
    )
    exec_id = uuid.UUID(ctx["execucao_id"])
    try:
        nos_result = await _rodar_nos(
            exec_id, ctx, fontes_execucao=fontes_execucao, sem_publicar=sem_publicar
        )
        await finalizar(execucao_id=exec_id, sem_publicar=sem_publicar)
        return {
            "id": str(exec_id),
            "status": "concluida",
            "versao": ctx["versao"],
            "nos": nos_result,
        }
    except ErroAplicacao as exc:
        await finalizar(execucao_id=exec_id, erro=exc.mensagem)
        # Keeps RAISING: `executar_encadeado` relies on this to abort the chain (running the
        # downstream over a failed upstream is worse than stopping). What is added is the run id,
        # which already exists and has a trail; the HTTP edge uses it to return 200 + id instead
        # of an envelope with no way back. An extra attribute, not an envelope field: `detalhes` is
        # an error contract and would gain an item no other error has.
        exc.execucao_id = str(exec_id)
        raise


async def _rodar_nos(
    exec_id: uuid.UUID,
    ctx: dict,
    *,
    fontes_execucao: dict | None,
    forcar_no: str | None = None,
    sem_publicar: bool = False,
) -> list[dict]:
    """The context's nodes by DEPENDENCY, with the same schedule as `ExecutarPipeline`
    (`execucao.paralelo`): each node starts when ITS predecessors have completed, up to
    `MAX_NOS_EM_PARALELO` at a time, and effect nodes or outputs outside the lake still run one at
    a time. `ctx["ordem"]` may be PARTIAL (re-run); the upstream outside it is already completed.
    The result comes back in topological order, for callers that read the response in sequence."""
    ordem = list(ctx["ordem"])
    resultados: dict[str, dict] = {}

    async def _no(nid: str) -> None:
        resultados[nid] = await executar_no(
            execucao_id=exec_id,
            definicao=ctx["definicao"],
            no_id=nid,
            parametros=ctx["parametros"],
            fontes_execucao=fontes_execucao,
            forcar=(nid == forcar_no),
            sem_publicar=sem_publicar,
        )

    nos = {n.get("id"): n for n in ctx["definicao"].get("nos") or []}
    await rodar_por_dependencia(
        ordem,
        dependencias_dos_nos(ctx["definicao"], ordem),
        _no,
        limite=MAX_NOS_EM_PARALELO,
        # An output that only publishes to the lake waits on network, not CPU: its own cap (`paralelo`).
        io=lambda nid: so_publica(nos.get(nid) or {}),
        limite_io=MAX_NOS_IO_EM_PARALELO,
    )
    return [resultados[n] for n in ordem]


async def rodar_ctx(
    ctx: dict,
    *,
    fontes_execucao: dict | None = None,
    forcar_no: str | None = None,
    sem_publicar: bool = False,
) -> dict:
    """Runs the nodes of an ALREADY-CREATED context (by `criar_execucao` or by the re-run), in the
    `ordem` the context itself carries, which may be PARTIAL.

    It exists because the re-run creates the run, seeds the upstream and only then runs: `executar`
    does all three at once and always with the full order. Error handling is the same as in
    `executar` (finalizes the run and returns the id along with the exception), because the screen
    of whoever re-ran needs the same way back.

    `sem_publicar`: the re-run of an upstream whose root declares `montante_publica: false`
    (`reexecucao.preparar`), the same signal `executar` forwards, down to `executar_no` and
    `finalizar`. Without it the re-run would publish with the as-of of a run declared as
    non-publishing."""
    exec_id = uuid.UUID(ctx["execucao_id"])
    try:
        nos_result = await _rodar_nos(
            exec_id,
            ctx,
            fontes_execucao=fontes_execucao,
            forcar_no=forcar_no,
            sem_publicar=sem_publicar,
        )
        await finalizar(execucao_id=exec_id, sem_publicar=sem_publicar)
        return {
            "id": str(exec_id),
            "status": "concluida",
            "versao": ctx["versao"],
            "nos": nos_result,
        }
    except ErroAplicacao as exc:
        await finalizar(execucao_id=exec_id, erro=exc.mensagem)
        exc.execucao_id = str(exec_id)
        raise


async def executar_encadeado(
    *,
    pipeline_id: uuid.UUID,
    versao_numero: int | None = None,
    ator_id: uuid.UUID,
    parametros: dict | None = None,
    pagina_id: uuid.UUID | None = None,
    origem: str = "manual",
    agendamento_id: uuid.UUID | None = None,
    ordem: list[dict] | None = None,
    ip: str | None = None,
    request_id: str | None = None,
) -> dict:
    """CHAINED run (pipeline DAG, INLINE): resolves the dependency closure (sources of type
    `pipeline`), runs each pipeline in topological order (dependencies first) and injects the
    freshness map `{pipeline_id -> execucao_id}` so the root reads the EXACT snapshots just produced.

    The SAME `parametros` (unit/as_of/period) flow to the whole chain; each pipeline validates only
    those it declares. Returns the root's result plus the list of the chain's runs.
    The Temporal path (worker) uses `executar` with `fontes_execucao` in the same order.

    An already-resolved `ordem` can be passed by whoever just called `resolver_ordem` (the schedule
    trigger): resolving twice opens a window in which someone publishes an upstream BETWEEN the two
    reads, and the chain that runs is no longer the one that was decided, including the one that
    passed the parameter and credential refusals.
    """
    from ..db import transacao as _transacao
    from ..pipelines.dag import resolver_ordem

    if ordem is None:
        async with _transacao() as conn:
            ordem = await resolver_ordem(conn, pipeline_id, versao_numero)
    fontes_execucao: dict[str, str] = {}
    raiz = str(pipeline_id)
    from ..pipelines.definicao import validar_parametros_da_cadeia
    from .disparo import planejar_montante

    # The SAME decision as the Temporal path (`disparo.preparar_cadeia`), based on the ROOT's
    # definition, and before any run exists: refusing an upstream effect leaves nothing behind.
    sem_publicar = await planejar_montante(ordem, raiz)

    # The root is the only one with a concurrency key; check BEFORE running the upstreams, or they
    # run in full only for the root to get a 409 at the end.
    #
    # With the parameters TRIMMED to what the root declares, not the chain's: in a closure, a name
    # may legitimately exist only upstream (an upstream runs per unit, without `competencia`), and
    # the pre-check with the whole map rejects the chain with `parametros_invalidos` because the
    # root does not know that name. Worse than the false 400: the concurrency key is
    # `md5(pipeline || parameters)`, so checking with one map and creating with another would look
    # for an in-flight run that never exists.
    raiz_def = next((i["definicao"] for i in ordem if i["pipeline_id"] == raiz), None)
    await verificar_raiz_disponivel(
        pipeline_id=pipeline_id,
        versao_numero=versao_numero,
        parametros=(
            filtrar_parametros_declarados(raiz_def, parametros) if raiz_def else parametros
        ),
        pagina_id=pagina_id,
        origem=origem,
        ator_id=ator_id,
        ip=ip,
        request_id=request_id,
    )
    cadeia_id = uuid.uuid4()  # same trigger -> enables `fonte` reuse across sibling pipelines

    # A name that NO pipeline in the chain declares is a loud error, as in a solo run: per-pipeline
    # trimming must not become a silent discard (see `validar_parametros_da_cadeia`).
    validar_parametros_da_cadeia([i["definicao"] for i in ordem], parametros)

    resultados: dict[str, dict] = {}

    async def _rodar_item(item: dict) -> None:
        pid = item["pipeline_id"]
        # Each pipeline in the chain only receives the parameters it DECLARES (an upstream may
        # declare a subset, e.g. per-unit without `competencia`). Avoids a 400 parametros_invalidos.
        params_pipe = filtrar_parametros_declarados(item["definicao"], parametros)
        res = await executar(
            pipeline_id=uuid.UUID(pid),
            versao_numero=item["versao_numero"],
            ator_id=ator_id,
            parametros=params_pipe,
            pagina_id=pagina_id if pid == raiz else None,
            origem=origem,
            agendamento_id=agendamento_id if pid == raiz else None,
            fontes_execucao=fontes_execucao,
            cadeia_id=cadeia_id,
            # The root is the REQUESTED run; the others are dependencies that ran as required tables.
            # The listing uses it as the group header; without the mark it would pick by heuristic
            # and could invert the reading (the dependency showing up as the caller).
            cadeia_raiz=(pid == raiz),
            ip=ip,
            request_id=request_id,
            sem_publicar=sem_publicar and pid != raiz,
        )
        # Only read by whoever DEPENDS on this pipeline, which starts only after it finishes; each
        # coroutine writes its own key.
        fontes_execucao[pid] = res["id"]
        resultados[pid] = res

    # By DEPENDENCY, the same function as `ExecutarDAG` (`execucao.paralelo`): each pipeline starts
    # when the ones it reads have finished, not when its whole level has. After the first failure
    # nothing new starts, what is in flight finishes (or it would stay 'executando' forever), and
    # the first failure is raised. An `ordem` built without `deps` (outside `resolver_ordem`) falls
    # back to the level barrier: depending on every lower-level item is exactly that.
    em_nivel = sorted(ordem, key=lambda it: it["nivel"])
    por_pid = {item["pipeline_id"]: item for item in em_nivel}
    deps = {
        pid: (
            item["deps"]
            if "deps" in item
            else [o["pipeline_id"] for o in em_nivel if o["nivel"] < item["nivel"]]
        )
        for pid, item in por_pid.items()
    }
    await rodar_por_dependencia(list(por_pid), deps, lambda pid: _rodar_item(por_pid[pid]))

    # `cadeia`/root rebuilt from the original ORDER (not the gather's completion order), keeping the
    # response contract identical to the pre-parallelism one.
    cadeia = [
        {
            "pipeline_id": i["pipeline_id"],
            "execucao_id": resultados[i["pipeline_id"]]["id"],
            "status": resultados[i["pipeline_id"]]["status"],
        }
        for i in ordem
    ]
    return {**resultados[raiz], "cadeia": cadeia}


async def listar(
    conn: AsyncConnection, pipeline_id: uuid.UUID, *, limite: int = 25, offset: int = 0
) -> dict:
    """Runs of a pipeline (most recent first), PAGINATED, for the UI/panel.

    Returns `{itens, total, limite, offset}`: `itens` is the current page (ordered by
    iniciada_em desc, uses the idx_exec_pipeline index); `total` lets the UI build pagination
    and reach the whole history. `limite` is clamped to [1, 100]."""
    limite = max(1, min(int(limite), 100))
    offset = max(0, int(offset))
    total = (
        await conn.execute(
            text("SELECT count(*) FROM execucoes WHERE pipeline_id = :p"), {"p": pipeline_id}
        )
    ).scalar_one()
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT e.id, e.versao_numero, e.status, e.iniciada_em, e.concluida_em, "
                    "       e.pagina_id, p.nome AS pagina_nome, u.nome AS ator_nome "
                    "FROM execucoes e "
                    "LEFT JOIN paginas p ON p.id = e.pagina_id "
                    "LEFT JOIN usuarios u ON u.id = e.iniciada_por "
                    "WHERE e.pipeline_id = :p ORDER BY e.iniciada_em DESC LIMIT :lim OFFSET :off"
                ),
                {"p": pipeline_id, "lim": limite, "off": offset},
            )
        )
        .mappings()
        .all()
    )
    itens = [
        {
            "id": str(r["id"]),
            "versao": r["versao_numero"],
            "status": r["status"],
            "iniciada_em": r["iniciada_em"].isoformat() if r["iniciada_em"] else None,
            "concluida_em": r["concluida_em"].isoformat() if r["concluida_em"] else None,
            "pagina_id": str(r["pagina_id"]) if r["pagina_id"] else None,
            "pagina_nome": r["pagina_nome"],
            "ator_nome": r["ator_nome"],
        }
        for r in rows
    ]
    return {"itens": itens, "total": int(total), "limite": limite, "offset": offset}


async def listar_global(
    conn: AsyncConnection,
    *,
    pipeline_id: uuid.UUID | None = None,
    status: str | None = None,
    origem: str | None = None,
    de: datetime | None = None,
    ate: datetime | None = None,
    busca: str | None = None,
    aba: str | None = None,
    limite: int = 25,
    offset: int = 0,
    incluir_sistema: bool = False,
) -> dict:
    """Runs of ALL pipelines (or of one, with `pipeline_id`), filterable: the Studio history
    screen.

    `busca` is free text (see `_predicado_busca`) and `aba` filters by the PIPELINE's classification
    with the same contract as the pipeline listing tabs (`ABAS_EXECUCAO`).

    All filters are optional and combinable: `status` (executando/concluida/falha), `origem`
    (manual/agendado) and the `[de, ate)` window over `iniciada_em`, with an **EXCLUSIVE end**, like
    the rest of the product (see `resolverJanela` in the front end). `incluir_sistema=False` hides
    runs of pipelines flagged `sistema` (admin only), in both the items and the `total`.

    Orders by `iniciada_em DESC, id DESC` (uses idx_exec_iniciada; `id` breaks ties and keeps
    pagination stable). `limite` is clamped to [1, 100]."""
    limite = max(1, min(int(limite), 100))
    offset = max(0, int(offset))
    # `cond` = the PAGE's filters; `cond_sem_status` = the same WITHOUT status, to count how many
    # there are of each status within the rest of the filter (the number the UI shows on each chip).
    cond: list[str] = []
    cond_sem_status: list[str] = []
    # VISIBILITY is different from FILTER, and the distinction became critical with run grouping:
    # the screen filter (status/origin/window/pipeline) does NOT trim the group's rows; it only
    # sets `casa_filtro`, so the parent can head the child's failure. Visibility ALWAYS trims, in
    # every query: without repeating this predicate in the items SELECT, a visible run drags the
    # `sistema` sibling pipelines of the same chain into the response, exposing the name and status
    # of a pipeline that `?pipeline_id=` denies with 403 to the same user.
    cond_visivel: list[str] = []
    base: dict = {}
    if not incluir_sistema:
        cond.append("pl.sistema = false")
        cond_sem_status.append("pl.sistema = false")
        cond_visivel.append("pl.sistema = false")
    if pipeline_id is not None:
        cond.append("e.pipeline_id = :p")
        cond_sem_status.append("e.pipeline_id = :p")
        base["p"] = pipeline_id
    if status:
        # Explicit CAST: the asyncpg codec does not infer the status_execucao enum from a str.
        cond.append("e.status = CAST(:st AS status_execucao)")
        base["st"] = status
    if origem:
        cond.append("e.origem = :og")
        cond_sem_status.append("e.origem = :og")
        base["og"] = origem
    if de is not None:
        cond.append("e.iniciada_em >= :de")
        cond_sem_status.append("e.iniciada_em >= :de")
        base["de"] = de
    if ate is not None:
        cond.append("e.iniciada_em < :ate")
        cond_sem_status.append("e.iniciada_em < :ate")
        base["ate"] = ate
    if busca and busca.strip():
        pred, binds = _predicado_busca(busca)
        cond.append(pred)
        cond_sem_status.append(pred)
        base.update(binds)
    # `cond_sem_aba` = the whole page MINUS the tab (status included): the number for each tab.
    cond_sem_aba = list(cond)
    if aba:
        if aba not in ABAS_EXECUCAO:
            raise ErroAplicacao(codigo="aba_invalida", mensagem=f"Unknown tab: {aba}.", status=400)
        cond.append(ABAS_EXECUCAO[aba])
        cond_sem_status.append(ABAS_EXECUCAO[aba])
    onde = " AND ".join(cond) if cond else "true"
    visivel = " AND ".join(cond_visivel) if cond_visivel else "true"
    # The PAGE is made of RUNS (chains), not executions: a pipeline with a `pipeline` source
    # triggers the whole chain, and paginating by execution would split the run in half, with the
    # parent on one page and the dependencies on the next, and nothing saying they came from the
    # same click. The group key is `cadeia_id`; a standalone execution is a group of one (COALESCE
    # with its own id).
    #
    # The count uses the SAME where (and the same join with pipelines); if they diverge, pagination lies.
    total = (
        await conn.execute(
            text(
                "SELECT count(DISTINCT COALESCE(e.cadeia_id::text, e.id::text)) FROM execucoes e "
                f"JOIN pipelines pl ON pl.id = e.pipeline_id WHERE {onde}"
            ),
            base,
        )
    ).scalar_one()
    # Two steps, not one: (1) pick the page's runs by the filter; (2) fetch ALL executions of those
    # runs, including those that do NOT match the filter. This lets the parent row head the group
    # when only the child matched (filtering 'falha' when the root completed); hiding the parent
    # would leave the failure orphaned, without saying which run it belongs to. Each row says
    # whether it matches (`casa_filtro`), and the screen shows "N of M match the filter".
    chaves = [
        r["chave"]
        for r in (
            (
                await conn.execute(
                    text(
                        "SELECT COALESCE(e.cadeia_id::text, e.id::text) AS chave, "
                        "       max(e.iniciada_em) AS m "
                        "FROM execucoes e JOIN pipelines pl ON pl.id = e.pipeline_id "
                        f"WHERE {onde} "
                        "GROUP BY 1 ORDER BY m DESC, chave DESC LIMIT :lim OFFSET :off"
                    ),
                    {**base, "lim": limite, "off": offset},
                )
            )
            .mappings()
            .all()
        )
    ]
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT e.id, e.pipeline_id, pl.nome AS pipeline_nome, e.versao_numero, "
                    "       e.status, left(e.erro, 300) AS erro, e.iniciada_em, e.concluida_em, "
                    "       e.origem, e.agendamento_id, a.nome AS agendamento_nome, "
                    "       e.pagina_id, pg.nome AS pagina_nome, u.nome AS ator_nome, "
                    "       e.cadeia_id, e.cadeia_raiz, e.reexecucao_de, "
                    "       COALESCE(e.cadeia_id::text, e.id::text) AS chave, "
                    # REAL start: the first node that began. Within a run, `iniciada_em` is the TRIGGER
                    # instant for all of them (pre-creation), and measuring duration from it adds the
                    # dependencies' queue time to the work (e.g. 78 s on screen vs 2 s of node time).
                    # A reused node (reuse or re-run copy) skips the start mark and only has
                    # `concluido_em`, written INSIDE this execution; LEAST ignores NULL.
                    "       (SELECT LEAST(min(n.iniciado_em), min(n.concluido_em)) "
                    "          FROM execucao_nos n WHERE n.execucao_id = e.id) AS comecou_em, "
                    f"       ({onde}) AS casa_filtro "
                    "FROM execucoes e "
                    "JOIN pipelines pl ON pl.id = e.pipeline_id "
                    "LEFT JOIN paginas pg ON pg.id = e.pagina_id "
                    "LEFT JOIN usuarios u ON u.id = e.iniciada_por "
                    "LEFT JOIN agendamentos a ON a.id = e.agendamento_id "
                    "WHERE COALESCE(e.cadeia_id::text, e.id::text) = ANY(:chaves) "
                    # The visibility filter REPEATED here: the keys were chosen with it, but the
                    # group brings the sibling executions, and some may be `sistema`.
                    f"  AND {visivel} "
                    "ORDER BY e.iniciada_em DESC, e.id DESC"
                ),
                {**base, "chaves": chaves},
            )
        )
        .mappings()
        .all()
        if chaves
        else []
    )
    # Per-status counts in the SAME query/transaction: the UI showed one number per chip and each
    # came from its own request (4 per load), which beyond the cost left the totals inconsistent
    # with each other, since executions start and finish between calls.
    onde_sem_status = " AND ".join(cond_sem_status) if cond_sem_status else "true"
    tot = (
        (
            await conn.execute(
                text(
                    "SELECT count(*) AS todas, "
                    "  count(*) FILTER (WHERE e.status='concluida') AS concluida, "
                    "  count(*) FILTER (WHERE e.status='falha') AS falha, "
                    "  count(*) FILTER (WHERE e.status='executando') AS executando "
                    "FROM execucoes e JOIN pipelines pl ON pl.id = e.pipeline_id "
                    f"WHERE {onde_sem_status}"
                ),
                {k: v for k, v in base.items() if k != "st"},
            )
        )
        .mappings()
        .first()
    )
    # How many EXECUTIONS there are in each tab with the other filters applied: the tab ruler's
    # number. A single SELECT, with each tab's predicate, so the tabs cannot diverge.
    onde_sem_aba = " AND ".join(cond_sem_aba) if cond_sem_aba else "true"
    por_aba = (
        (
            await conn.execute(
                text(
                    "SELECT count(*) AS todas, "
                    + ", ".join(
                        f"count(*) FILTER (WHERE {pred}) AS {chave}"
                        for chave, pred in ABAS_EXECUCAO.items()
                    )
                    + " FROM execucoes e JOIN pipelines pl ON pl.id = e.pipeline_id "
                    f"WHERE {onde_sem_aba}"
                ),
                base,
            )
        )
        .mappings()
        .first()
    )
    itens = [
        {
            "id": str(r["id"]),
            "pipeline_id": str(r["pipeline_id"]),
            "pipeline_nome": r["pipeline_nome"],
            "versao": r["versao_numero"],
            "status": r["status"],
            "erro": r["erro"],
            "iniciada_em": r["iniciada_em"].isoformat() if r["iniciada_em"] else None,
            "concluida_em": r["concluida_em"].isoformat() if r["concluida_em"] else None,
            # NULL = no node started (still queued behind dependencies, or the run stopped earlier).
            "comecou_em": r["comecou_em"].isoformat() if r["comecou_em"] else None,
            "origem": r["origem"],
            "agendamento_id": str(r["agendamento_id"]) if r["agendamento_id"] else None,
            "agendamento_nome": r["agendamento_nome"],
            "pagina_id": str(r["pagina_id"]) if r["pagina_id"] else None,
            "pagina_nome": r["pagina_nome"],
            "ator_nome": r["ator_nome"],
            "cadeia_id": str(r["cadeia_id"]) if r["cadeia_id"] else None,
            "cadeia_raiz": bool(r["cadeia_raiz"]),
            "reexecucao_de": str(r["reexecucao_de"]) if r["reexecucao_de"] else None,
            "casa_filtro": bool(r["casa_filtro"]),
        }
        for r in rows
    ]
    # Groups while preserving the order of the runs chosen in step 1 (the items SELECT reorders by
    # execution, not by group). Within the group, the ROOT is the one flagged `cadeia_raiz`; for
    # chains that predate the flag, the most RECENT wins, because the chained trigger creates the
    # dependencies first and the root last.
    por_chave: dict[str, list[tuple]] = {}
    for it, r in zip(itens, rows, strict=True):
        por_chave.setdefault(r["chave"], []).append((it, r))
    grupos = []
    for chave in chaves:
        pares = por_chave.get(chave, [])
        if not pares:
            continue
        membros = [m for m, _ in pares]
        raiz = next((m for m in membros if m["cadeia_raiz"]), membros[0])
        # Children in the ORDER THEY RAN (real start), not by `iniciada_em`, which is the same for
        # the whole run and left the order arbitrary. Those not yet started go last. The key compares
        # the raw datetime (not the ISO text), and the `is None` flag comes before each value so
        # None is never compared with a datetime.
        filhas = [
            m
            for m, _ in sorted(
                (p for p in pares if p[0]["id"] != raiz["id"]),
                key=lambda p: (
                    p[1]["comecou_em"] is None,
                    p[1]["comecou_em"],
                    p[1]["concluida_em"] is None,
                    p[1]["concluida_em"],
                    p[0]["pipeline_nome"],
                ),
            )
        ]
        grupos.append(
            {
                "chave": chave,
                "raiz": raiz,
                "filhas": filhas,
                "total_no_grupo": len(membros),
                "casam_filtro": sum(1 for m in membros if m["casa_filtro"]),
            }
        )
    return {
        # One run per item. A single-execution group comes with `filhas: []` and the screen draws it
        # exactly as before; only a chain gets an accordion.
        "itens": grupos,
        "total": int(total),  # RUNS, not executions: what pagination walks over
        "limite": limite,
        "offset": offset,
        # `totais` counts EXECUTIONS and ignores the status filter (the other filters apply); it is
        # what each chip shows. Counting runs here would answer another question: someone looking
        # for "3 failures" wants three failed executions, even if all three are in the same run.
        "totais": {k: int(tot[k]) for k in ("todas", "concluida", "falha", "executando")}
        if tot
        else None,
        # Executions per tab (`todas` + one key per tab), with every filter except the tab.
        "totais_abas": {k: int(v) for k, v in por_aba.items()} if por_aba else None,
    }


# Tabs of the Executions screen: the SAME contract as the tabs of the pipeline listing, applied to
# the pipeline of each execution. "geral" is "none of the others"; the others overlap on purpose
# (a pipeline can be gold AND belong to a group). Keys and SQL are constants - nothing from the
# user enters the text.
ABAS_EXECUCAO: dict[str, str] = {
    "geral": "(pl.camada IS NULL AND pl.grupo IS NULL AND pl.sistema = false)",
    "sistema": "pl.sistema = true",
    "bronze": "pl.camada = 'bronze'",
    "silver": "pl.camada = 'silver'",
    "gold": "pl.camada = 'gold'",
    "load": "pl.grupo = 'load'",
    "reference": "pl.grupo = 'reference'",
    "export": "pl.grupo = 'export'",
}

_TERMO_PARAMETRO = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.+)$")
_MAX_TERMOS_BUSCA = 6


def _escapar_like(t: str) -> str:
    return t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _predicado_busca(busca: str) -> tuple[str, dict]:
    """Free text from the Executions screen -> SQL predicate + binds.

    Each word must match (AND) in SOME of these fields (OR): the name of the pipeline, schedule,
    page and triggering user, the error, the parameters (already redacted) and the id of the
    execution or run (prefix). `key=value` matches the EXACT parameter (`unit_id=188`); a bare
    "188" would match any parameter, id or error containing the number. Every value goes through
    a bind; a typed `%`/`_` is escaped, so it is literal."""
    termos = busca.split()[:_MAX_TERMOS_BUSCA]
    partes: list[str] = []
    binds: dict = {}
    for i, termo in enumerate(termos):
        m = _TERMO_PARAMETRO.match(termo)
        if m:
            binds[f"bk{i}"], binds[f"bv{i}"] = m.group(1), m.group(2)
            partes.append(f"(e.parametros ->> :bk{i}) = :bv{i}")
            continue
        binds[f"b{i}"] = f"%{_escapar_like(termo)}%"
        binds[f"bp{i}"] = f"{_escapar_like(termo.lower())}%"
        partes.append(
            "("
            f"pl.nome ILIKE :b{i} OR e.erro ILIKE :b{i} OR e.parametros::text ILIKE :b{i} "
            f"OR e.id::text LIKE :bp{i} OR e.cadeia_id::text LIKE :bp{i} "
            "OR EXISTS (SELECT 1 FROM agendamentos ab WHERE ab.id = e.agendamento_id "
            f"  AND ab.nome ILIKE :b{i}) "
            "OR EXISTS (SELECT 1 FROM paginas pb WHERE pb.id = e.pagina_id "
            f"  AND pb.nome ILIKE :b{i}) "
            "OR EXISTS (SELECT 1 FROM usuarios ub WHERE ub.id = e.iniciada_por "
            f"  AND ub.nome ILIKE :b{i})"
            ")"
        )
    return (" AND ".join(partes) if partes else "true"), binds


async def consultar_corrida(
    conn: AsyncConnection, cadeia_id: uuid.UUID, *, incluir_sistema: bool = False
) -> dict:
    """A whole RUN (chain) as a map: the chain's executions and who depends on whom.

    The edges are not stored by the trigger (the `deps` travel only in the workflow payload); they
    are rebuilt from the definitions of the VERSIONS THAT RAN, by the same rules as the trigger
    (`dag.arestas_da_cadeia`). A run has one execution per pipeline (a re-run is born outside a
    chain), so `pipeline_id` identifies the execution.

    Same VISIBILITY as `listar_global`: without admin, a `sistema` pipeline does not appear, and
    the hidden node is bridged (`dag.restringir_arestas`) so neighbours do not look independent.
    A nonexistent run and a run with nothing visible answer the SAME 404: telling them apart would
    reveal that the id exists.

    The items have the same shape as those of `GET /execucoes`, so the screen can reuse the run
    logic (`tempoNaCorrida`, `janelaCorrida`). The query is its own, not a slice of `listar_global`,
    to leave the executions list untouched.
    """
    from ..pipelines import dag  # late import: `dag` pulls in the pipelines service

    rows = (
        (
            await conn.execute(
                text(
                    "SELECT e.id, e.pipeline_id, pl.nome AS pipeline_nome, pl.sistema, "
                    "       e.versao_numero, e.status, left(e.erro, 300) AS erro, "
                    "       e.iniciada_em, e.concluida_em, e.origem, e.agendamento_id, "
                    "       a.nome AS agendamento_nome, e.pagina_id, pg.nome AS pagina_nome, "
                    "       u.nome AS ator_nome, e.cadeia_id, e.cadeia_raiz, e.reexecucao_de, "
                    # REAL start (the first node that began): same computation as `listar_global`.
                    "       (SELECT LEAST(min(n.iniciado_em), min(n.concluido_em)) "
                    "          FROM execucao_nos n WHERE n.execucao_id = e.id) AS comecou_em, "
                    "       v.definicao "
                    "FROM execucoes e "
                    "JOIN pipelines pl ON pl.id = e.pipeline_id "
                    "LEFT JOIN pipeline_versoes v "
                    "  ON v.pipeline_id = e.pipeline_id AND v.numero = e.versao_numero "
                    "LEFT JOIN paginas pg ON pg.id = e.pagina_id "
                    "LEFT JOIN usuarios u ON u.id = e.iniciada_por "
                    "LEFT JOIN agendamentos a ON a.id = e.agendamento_id "
                    "WHERE e.cadeia_id = :c "
                    "ORDER BY e.iniciada_em, e.id"
                ),
                {"c": cadeia_id},
            )
        )
        .mappings()
        .all()
    )
    visiveis_rows = [r for r in rows if incluir_sistema or not r["sistema"]]
    if not visiveis_rows:
        raise ErroAplicacao(codigo="corrida_inexistente", mensagem="Run not found.", status=404)

    # The root is the flagged one; in a chain that predates the flag, the most RECENT, the same
    # rule as `listar_global`, which draws the run header in the list.
    raiz = next((r for r in rows if r["cadeia_raiz"]), rows[-1])
    exec_por_pid: dict[str, list[str]] = {}
    definicoes: dict[str, dict] = {}
    for r in rows:
        pid = str(r["pipeline_id"])
        exec_por_pid.setdefault(pid, []).append(str(r["id"]))
        bruta = r["definicao"]
        definicoes.setdefault(pid, (json.loads(bruta) if isinstance(bruta, str) else bruta) or {})
    todas = dag.arestas_da_cadeia(
        [{"pipeline_id": p, "definicao": d} for p, d in definicoes.items()],
        raiz=str(raiz["pipeline_id"]),
    )
    visiveis = {str(r["pipeline_id"]) for r in visiveis_rows}
    arestas = [
        {"de": de_exec, "para": para_exec, "tipo": tipo}
        for pid, deps in dag.restringir_arestas(todas, visiveis).items()
        for dep, tipo in deps.items()
        for de_exec in exec_por_pid[dep]
        for para_exec in exec_por_pid[pid]
    ]

    def _iso(v):
        return v.isoformat() if v else None

    execucoes = [
        {
            "id": str(r["id"]),
            "pipeline_id": str(r["pipeline_id"]),
            "pipeline_nome": r["pipeline_nome"],
            "versao": r["versao_numero"],
            "status": r["status"],
            "erro": r["erro"],
            "iniciada_em": _iso(r["iniciada_em"]),
            "concluida_em": _iso(r["concluida_em"]),
            "comecou_em": _iso(r["comecou_em"]),
            "origem": r["origem"],
            "agendamento_id": str(r["agendamento_id"]) if r["agendamento_id"] else None,
            "agendamento_nome": r["agendamento_nome"],
            "pagina_id": str(r["pagina_id"]) if r["pagina_id"] else None,
            "pagina_nome": r["pagina_nome"],
            "ator_nome": r["ator_nome"],
            "cadeia_id": str(r["cadeia_id"]),
            "cadeia_raiz": bool(r["cadeia_raiz"]),
            "reexecucao_de": str(r["reexecucao_de"]) if r["reexecucao_de"] else None,
            # No screen filter here: everything visible "matches". Keeps the shape of `GET /execucoes`.
            "casa_filtro": True,
        }
        for r in visiveis_rows
    ]
    return {
        "cadeia_id": str(cadeia_id),
        # `sistema` pipeline root for a non-admin: the run appears without a named header.
        "raiz_id": str(raiz["id"]) if str(raiz["pipeline_id"]) in visiveis else None,
        "execucoes": execucoes,
        "arestas": sorted(arestas, key=lambda a: (a["para"], a["de"])),
    }


async def execucoes_do_dia(
    conn: AsyncConnection,
    *,
    pipeline_id: uuid.UUID,
    pagina_id: uuid.UUID | None = None,
    parametros: dict | None = None,
    parametros_contidos: dict | None = None,
) -> list[dict]:
    """COMPLETED runs of TODAY (America/Sao_Paulo) for this pipeline: the day's cache.

    Two questions, and they are not the same:

    - `parametros`: "is there a run with THESE parameters?". Exact equality. It is the Portal's
      report cache ('see existing / history / generate new'), and remains the default.
    - `parametros_contidos`: "is there a run that has THESE keys, whatever the others are?".
      Containment (`@>`). It is the nightly-load question: *is there a load from today for unit
      X?* Its as-of is what we want to DISCOVER, so requiring it in the question would require the
      answer. Normalized by `normalizar_parcial`, which rejects an empty filter (`@>` with `{}`
      matches any run).

    `pagina_id` is OPTIONAL. With a page, the scope is the usual one. Without, the answer covers
    what was triggered by the API and by the scheduler, which is exactly where the nightly load
    comes from, and where `pagina_id` is NULL.

    Returns the candidates, most recent first, with each one's `parametros`: that is where the
    caller takes the as-of to reuse.
    """
    if parametros is not None and parametros_contidos is not None:
        raise ErroAplicacao(
            codigo="parametros_invalidos",
            mensagem="Search by equality OR by containment, not both.",
            status=400,
        )
    det = await ps.obter(conn, pipeline_id, None)
    if det["numero"] == 0:
        return []
    definicao = det["definicao"]
    # Redact before comparing: `execucoes.parametros` is stored redacted, so comparing with the
    # clear value would never match (the day's cache would silently stop working).
    if parametros_contidos is not None:
        params = redigir_segredos(definicao, normalizar_parcial(definicao, parametros_contidos))
        comparacao = "e.parametros @> CAST(:params AS jsonb)"
    else:
        resolvidos = await resolver_derivados(definicao, parametros)
        params = redigir_segredos(definicao, normalizar_parametros(definicao, resolvidos))
        comparacao = "e.parametros = CAST(:params AS jsonb)"
    filtro_pagina = "e.pagina_id = :pag AND " if pagina_id is not None else ""
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT e.id, e.iniciada_em, e.concluida_em, e.origem, e.agendamento_id, "
                    "       e.parametros, u.nome AS ator_nome "
                    "FROM execucoes e LEFT JOIN usuarios u ON u.id = e.iniciada_por "
                    f"WHERE e.pipeline_id = :p AND {filtro_pagina}e.status = 'concluida' "
                    f"  AND {comparacao} "
                    "  AND (e.iniciada_em AT TIME ZONE 'America/Sao_Paulo')::date "
                    "      = (now() AT TIME ZONE 'America/Sao_Paulo')::date "
                    "ORDER BY e.iniciada_em DESC"
                ),
                {"p": pipeline_id, "pag": pagina_id, "params": json.dumps(params)},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "id": str(r["id"]),
            "iniciada_em": r["iniciada_em"].isoformat() if r["iniciada_em"] else None,
            "concluida_em": r["concluida_em"].isoformat() if r["concluida_em"] else None,
            "origem": r["origem"],
            "agendamento_id": str(r["agendamento_id"]) if r["agendamento_id"] else None,
            "ator_nome": r["ator_nome"],
            "parametros": r["parametros"],
        }
        for r in rows
    ]


async def resumo_execucoes(
    conn: AsyncConnection,
    pipeline_id: uuid.UUID,
    *,
    agendamento_id: uuid.UUID | None = None,
    limite_dias: int = 60,
) -> dict:
    """SUMMARIZED run history grouped by DAY (America/Sao_Paulo): per day, total + breakdown by
    status (concluded/failed/running) and by origin (scheduled/manual) + time of the last one.
    With `agendamento_id`, scopes ONLY to the runs OF THAT schedule (the detail's History tab);
    without it, the whole pipeline. `total` (overall for the scope) makes the size clear."""
    onde = "pipeline_id = :p"
    base = {"p": pipeline_id}
    if agendamento_id is not None:
        onde += " AND agendamento_id = :a"
        base["a"] = agendamento_id
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT (iniciada_em AT TIME ZONE 'America/Sao_Paulo')::date AS dia, "
                    "       count(*) AS total, "
                    "       count(*) FILTER (WHERE status='concluida') AS concluidas, "
                    "       count(*) FILTER (WHERE status='falha') AS falhas, "
                    "       count(*) FILTER (WHERE status='executando') AS executando, "
                    "       count(*) FILTER (WHERE origem='agendado') AS agendadas, "
                    "       count(*) FILTER (WHERE origem='manual') AS manuais, "
                    "       max(iniciada_em) AS ultima "
                    f"FROM execucoes WHERE {onde} "
                    "GROUP BY dia ORDER BY dia DESC LIMIT :lim"
                ),
                {**base, "lim": max(1, min(int(limite_dias), 366))},
            )
        )
        .mappings()
        .all()
    )
    total = (
        await conn.execute(text(f"SELECT count(*) FROM execucoes WHERE {onde}"), base)
    ).scalar_one()
    dias = [
        {
            "dia": r["dia"].isoformat() if r["dia"] else None,
            "total": int(r["total"]),
            "concluidas": int(r["concluidas"]),
            "falhas": int(r["falhas"]),
            "executando": int(r["executando"]),
            "agendadas": int(r["agendadas"]),
            "manuais": int(r["manuais"]),
            "ultima": r["ultima"].isoformat() if r["ultima"] else None,
        }
        for r in rows
    ]
    return {"dias": dias, "total": int(total)}


async def listar_por_agendamento(
    conn: AsyncConnection, agendamento_id: uuid.UUID, *, dia: str | None = None, limite: int = 200
) -> list[dict]:
    """Runs of ONE schedule (each run), most recent first; `dia` (YYYY-MM-DD, SP) filters the day,
    a drill-down from History to see the 1st, 2nd, 3rd... that ran. Includes `parametros` (to show
    each run's unit in the fan-out)."""
    onde = "agendamento_id = :a"
    base: dict = {"a": agendamento_id}
    if dia:
        # Bind a `date` object (not a string): asyncpg requires date for the ::date codec.
        onde += " AND (iniciada_em AT TIME ZONE 'America/Sao_Paulo')::date = :dia"
        base["dia"] = _date.fromisoformat(dia)
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT id, iniciada_em, concluida_em, status, parametros "
                    f"FROM execucoes WHERE {onde} ORDER BY iniciada_em DESC LIMIT :lim"
                ),
                {**base, "lim": max(1, min(int(limite), 1000))},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "id": str(r["id"]),
            "iniciada_em": r["iniciada_em"].isoformat() if r["iniciada_em"] else None,
            "concluida_em": r["concluida_em"].isoformat() if r["concluida_em"] else None,
            "status": r["status"],
            "parametros": r["parametros"],
        }
        for r in rows
    ]


async def consultar(conn: AsyncConnection, execucao_id: uuid.UUID) -> dict:
    """Run state + per-node trail (tracing: run -> node -> snapshot)."""
    e = (
        (
            await conn.execute(
                text(
                    "SELECT e.id, e.pipeline_id, e.versao_numero, e.status, e.erro, "
                    "       e.iniciada_em, e.concluida_em, e.origem, e.agendamento_id, "
                    "       a.nome AS agendamento_nome, e.parametros, "
                    "       e.cadeia_id, e.cadeia_raiz, e.reexecucao_de, e.reexecucao_desde_no, "
                    "       e.pagina_id, p.nome AS pagina_nome, u.nome AS ator_nome "
                    "FROM execucoes e "
                    "LEFT JOIN paginas p ON p.id = e.pagina_id "
                    "LEFT JOIN usuarios u ON u.id = e.iniciada_por "
                    "LEFT JOIN agendamentos a ON a.id = e.agendamento_id "
                    "WHERE e.id = :id"
                ),
                {"id": execucao_id},
            )
        )
        .mappings()
        .first()
    )
    if e is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Run not found.", status=404)
    nos = (
        (
            await conn.execute(
                text(
                    "SELECT no_id, status, linhas, hash, snapshot, duracao_ms, reaproveitado, erro, "
                    "iniciado_em FROM execucao_nos "
                    # NULLS LAST is explicit on purpose: the in-progress node (no `concluido_em`)
                    # goes at the end of the trail, which is where it sits in time, not the start.
                    "WHERE execucao_id = :id ORDER BY concluido_em NULLS LAST, iniciado_em"
                ),
                {"id": execucao_id},
            )
        )
        .mappings()
        .all()
    )
    return {
        "id": str(e["id"]),
        "pipeline_id": str(e["pipeline_id"]),
        "versao": e["versao_numero"],
        "status": e["status"],
        "erro": e["erro"],
        "iniciada_em": e["iniciada_em"].isoformat() if e["iniciada_em"] else None,
        "concluida_em": e["concluida_em"].isoformat() if e["concluida_em"] else None,
        "origem": e["origem"],
        "agendamento_id": str(e["agendamento_id"]) if e["agendamento_id"] else None,
        "agendamento_nome": e["agendamento_nome"],
        # `execucoes.parametros` is persisted ALREADY REDACTED (see criar_execucao), so returning it
        # here exposes no secret; a `segredo: true` parameter value comes out as the redaction mark.
        "parametros": e["parametros"],
        "pagina_id": str(e["pagina_id"]) if e["pagina_id"] else None,
        "pagina_nome": e["pagina_nome"],
        "ator_nome": e["ator_nome"],
        "cadeia_id": str(e["cadeia_id"]) if e["cadeia_id"] else None,
        "cadeia_raiz": bool(e["cadeia_raiz"]),
        # Where this run came from, when it is a re-run: it links the new one to the evidence of
        # the one that failed (which stays intact, marked `falha`).
        "reexecucao_de": str(e["reexecucao_de"]) if e["reexecucao_de"] else None,
        "reexecucao_desde_no": e["reexecucao_desde_no"],
        "nos": [dict(n) for n in nos],
    }


async def preview_snapshot(
    conn: AsyncConnection, execucao_id: uuid.UUID, no_id: str, limite: int = 50
) -> dict:
    """Sample of ONE node's Parquet snapshot in a run + its log (status/error/metrics).
    Drill-down in Studio/Executions: click the node -> see the data and the full log (success or
    failure). `limite` is clamped to [1, 200] (the UI offers 50/100/200)."""
    limite = max(1, min(int(limite), 200))
    n = (
        (
            await conn.execute(
                text(
                    "SELECT no_id, status, linhas, hash, snapshot, duracao_ms, reaproveitado, erro, "
                    "iniciado_em FROM execucao_nos WHERE execucao_id = :e AND no_id = :n"
                ),
                {"e": execucao_id, "n": no_id},
            )
        )
        .mappings()
        .first()
    )
    if n is None:
        raise ErroAplicacao(
            codigo="no_nao_encontrado", mensagem="Node not found in this run.", status=404
        )
    info = dict(n)
    info["colunas"], info["amostra"], info["contagem"] = [], [], None
    snap = n["snapshot"]
    if snap and os.path.exists(snap):
        import duckdb

        d = duckdb.connect()
        try:
            cur = d.execute(f"SELECT * FROM read_parquet(?) LIMIT {int(limite)}", [snap])
            cols = [c[0] for c in cur.description]
            info["colunas"] = cols
            info["amostra"] = [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]
            info["contagem"] = int(
                d.execute("SELECT count(*) FROM read_parquet(?)", [snap]).fetchone()[0]
            )
        finally:
            d.close()
    return info
