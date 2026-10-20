"""SQL Server connector (python-tds / pytds: pure-Python TDS driver, MIT, no system driver).

pytds is synchronous; each operation runs in a thread (asyncio.to_thread) so the event loop is not
blocked. Errors are sanitised (no secret or connection string echoed).
"""

from __future__ import annotations

import asyncio
import re

from ...config import get_settings
from ...errors import ErroAplicacao
from .. import colunar, processos
from ..colunar import Extracao
from ..parametros import PYFORMAT, vincular
from ..pool import sqlserver_conn
from .base import (
    ResultadoTeste,
    conferir_escopo,
    parametro_do_escopo,
    serial,
    valores_do_escopo,
)

_COMENTARIO_LINHA = re.compile(r"^\s*--[^\n]*\n", re.MULTILINE)
_COMENTARIO_BLOCO = re.compile(r"/\*.*?\*/", re.DOTALL)
_ENTRADA = re.compile(r"\b_entrada\b")
_LITERAL_OU_COMENTARIO = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.DOTALL)


def _reescrever_entrada(sql: str) -> str:
    """In T-SQL the predecessor's staging is a SESSION temp table (`#entrada`), but the user writes
    `_entrada` (same contract as Postgres). Replaces `_entrada` -> `#entrada` ONLY in code: keeps
    occurrences inside string literals (`'_entrada'`) and comments (`-- _entrada`), which are not
    table references. A bracketed identifier (`[_entrada]`) is not a supported path; use the bare
    name `_entrada`."""
    partes: list[str] = []
    fim = 0
    for m in _LITERAL_OU_COMENTARIO.finditer(sql):
        partes.append(_ENTRADA.sub("#entrada", sql[fim : m.start()]))
        partes.append(m.group(0))  # literal/comment left intact
        fim = m.end()
    partes.append(_ENTRADA.sub("#entrada", sql[fim:]))
    return "".join(partes)


def _qb(ident: str) -> str:
    """Quotes a SQL Server identifier (brackets, with ]] escaped). The identifier is already validated
    (`^[A-Za-z_][A-Za-z0-9_]*$`) in the definition; quoting is extra defence."""
    return "[" + ident.replace("]", "]]") + "]"


def montar_sql_tabela_mssql(
    schema: str, tabela: str, colunas_destino: list[str], estrategia: str, chave: list[str] | None
) -> str:
    """Final SQL (pure) that moves the `#stage` staging table into the target, per strategy. The
    TRUNCATE of truncate_insert runs separately (same connection/transaction). Reused by preview and
    execution."""
    alvo = f"{_qb(schema)}.{_qb(tabela)}"
    cols = ", ".join(_qb(c) for c in colunas_destino)
    if estrategia in ("append", "truncate_insert"):
        return f"INSERT INTO {alvo} ({cols}) SELECT {cols} FROM #stage"
    if estrategia == "upsert":
        ch = chave or []
        on = " AND ".join(f"a.{_qb(c)} = s.{_qb(c)}" for c in ch)
        nonkey = [c for c in colunas_destino if c not in ch]
        vals = ", ".join(f"s.{_qb(c)}" for c in colunas_destino)
        matched = ""
        if nonkey:
            set_ = ", ".join(f"a.{_qb(c)} = s.{_qb(c)}" for c in nonkey)
            matched = f"WHEN MATCHED THEN UPDATE SET {set_} "
        # T-SQL MERGE requires a ';' terminator.
        return (
            f"MERGE {alvo} AS a USING #stage AS s ON ({on}) "
            f"{matched}WHEN NOT MATCHED THEN INSERT ({cols}) VALUES ({vals});"
        )
    raise ErroAplicacao(
        codigo="escrita_estrategia",
        mensagem=f"Strategy '{estrategia}' is not supported.",
        status=400,
    )


def _igualdades_escopo(escopo: dict[str, str]) -> str:
    return " AND ".join(f"{_qb(c)} = :{parametro_do_escopo(ref)}" for c, ref in escopo.items())


def montar_sql_limpeza_mssql(schema: str, tabela: str, escopo: dict[str, str]) -> str:
    """Scope DELETE (`limpar_escopo`), which runs in the SAME transaction as the write. `WITH
    (HOLDLOCK)` is SERIALIZABLE for this statement only: it locks the key RANGE of the scope until
    commit, and a second run of the same scope waits on its own DELETE instead of interleaving
    DELETE/MERGE with this one (the interleaving A.delete, B.delete, A.write, B.write left A's
    leftovers). A range, not a row: without it, a row A inserts after B's DELETE would not be locked.
    The value goes in as `:parametro` and `vincular` swaps it for the pytds `%s`, never into the text."""
    alvo = f"{_qb(schema)}.{_qb(tabela)}"
    return f"DELETE FROM {alvo} WITH (HOLDLOCK) WHERE {_igualdades_escopo(escopo)}"


def montar_sql_fora_do_escopo_mssql(escopo: dict[str, str]) -> str:
    """Counts the staging rows that do NOT belong to the cleared scope. A row outside the scope is
    never deleted by that scope's runs, so it is a leftover by construction. NULL counts as outside
    (`NULL = x` is unknown, and `NOT` alone would let it through). `#stage` inherits the target
    types, so the comparison is the same as the DELETE's."""
    nulos = " OR ".join(f"{_qb(c)} IS NULL" for c in escopo)
    return f"SELECT COUNT(*) AS n FROM #stage WHERE NOT ({_igualdades_escopo(escopo)}) OR {nulos}"


def _escalar(cur) -> int:
    """First column of the row. The connection is `as_dict=True`, and pytds keys the row by column
    NAME: an unnamed column becomes the key `"0"`, not index 0. A plain `cur.fetchone()[0]` reading
    `@@ROWCOUNT` raised `KeyError`, and the generic `except` turned it into `escrita_falhou` with a
    rollback, so every SQL Server write undid what it had just written."""
    linha = cur.fetchone()
    return int(next(iter(linha.values())) if isinstance(linha, dict) else linha[0])


_SELECT_OU_WITH = re.compile(r"\s*(SELECT|WITH)\b", re.IGNORECASE)
_OPTION = re.compile(r"\bOPTION\s*\(", re.IGNORECASE)


def _com_recompile(sql: str) -> str:
    """Appends `OPTION (RECOMPILE)` to EXECUTION reads: each run compiles ITS OWN plan.

    Without it, SQL Server reuses the plan compiled for the first parameter value that used the
    text, and in a nightly batch dozens of runs go through the same text, from the smallest input to
    the largest. Measured with Query Store: the queries alternate between two plans on every
    recompilation; on the night they got the bad one, logical reads tripled and the average
    duration went from ~170 s to ~930 s, exceeding the activity timeout on both attempts.
    Recompiling costs milliseconds; the extraction, minutes. It is the same reason (measured
    earlier) why `processos.sql_da_fatia` already recompiles each slice.

    Conservative on purpose: only a single `SELECT`/`WITH` statement, with no `OPTION` of its own.
    `EXEC`, a batch with more than one statement, or an existing `OPTION` pass through untouched:
    the hint at the end of an `EXEC` or on the wrong statement of a batch is invalid T-SQL, and
    replacing the `OPTION` of someone who wrote one would be deciding for them. The hint goes on a
    NEW LINE: a trailing `-- comment` at the end of the SQL would swallow it."""
    codigo = _LITERAL_OU_COMENTARIO.sub(" ", sql).strip()
    ponto_e_virgula = codigo.endswith(";")
    if ponto_e_virgula:
        codigo = codigo[:-1]
    if not _SELECT_OU_WITH.match(codigo) or ";" in codigo or _OPTION.search(codigo):
        return sql
    corpo = sql.rstrip()
    if ponto_e_virgula:
        if not corpo.endswith(";"):
            return sql  # `...; -- comment`: the hint after the `;` would become a stray statement
        corpo = corpo[:-1].rstrip()
    return f"{corpo}\nOPTION (RECOMPILE)"


def _e_cte(sql: str) -> bool:
    """True if the SQL starts with a CTE (WITH ...), ignoring leading comments/whitespace.
    A CTE cannot be wrapped in a subquery (SELECT * FROM (WITH...) is invalid T-SQL)."""
    limpo = _COMENTARIO_BLOCO.sub("", _COMENTARIO_LINHA.sub("", sql)).lstrip()
    return bool(re.match(r"WITH\s", limpo, re.IGNORECASE))


def _causa(exc: Exception) -> str:
    import pytds

    msg = str(exc).lower()
    if isinstance(exc, pytds.LoginError):
        if "cannot open database" in msg:
            return "database does not exist"
        return "authentication refused"
    if "login failed" in msg or "password" in msg:
        return "authentication refused"
    if "cannot open database" in msg:
        return "database does not exist"
    if isinstance(exc, (OSError, ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return "host unreachable"
    if isinstance(exc, pytds.OperationalError):
        return "host unreachable"
    return "failed to connect"


#: Isolation requested on the connection (`config.isolamento`). CLOSED vocabulary: only
#: `snapshot`, and only for READS. It exists because the source database may receive loads from a
#: sink while the extraction scans the table: with `(nolock)` (dirty read, physical-order scan) a row
#: that moves page due to a split vanishes from the read with no error at all (measured: the same
#: extraction returned different row counts on the same day). Snapshot gives a picture of the
#: instant of the query without blocking the writer.
ISOLAMENTOS = ("snapshot",)
_SQL_ISOLAMENTO = {"snapshot": "SET TRANSACTION ISOLATION LEVEL SNAPSHOT"}
#: `sys.databases` state: 1 = ON, 2 = OFF_TO_ON (in transition, already usable for reads).
_SQL_ESTADO_SNAPSHOT = "SELECT snapshot_isolation_state FROM sys.databases WHERE name = DB_NAME()"


def _preparar_sessao(conn, config: dict, autocommit: bool) -> None:
    """Puts the session in the requested isolation. Only for READS (`autocommit=True`): for writes,
    snapshot trades blocking for an update-conflict error (3960), which is a different decision.

    Checks the database state BEFORE the query runs. Without the check, a database lacking
    `ALLOW_SNAPSHOT_ISOLATION` only fails on the first SELECT, with the raw 3952, in the middle of a
    multi-minute extraction, and the message does not say what to do."""
    isolamento = (config.get("isolamento") or "").strip().lower()
    if not isolamento or not autocommit:
        return
    if isolamento not in ISOLAMENTOS:
        raise ErroAplicacao(
            codigo="isolamento_invalido",
            mensagem=(
                f"Isolation {isolamento!r} does not exist on this connection. "
                f"Use one of: {', '.join(ISOLAMENTOS)}."
            ),
            status=400,
        )
    cur = conn.cursor()
    cur.execute(_SQL_ESTADO_SNAPSHOT)
    linha = cur.fetchone()
    estado = _primeiro_valor(linha)
    if estado not in (1, 2):
        raise ErroAplicacao(
            codigo="isolamento_snapshot_indisponivel",
            mensagem=(
                f"Database '{config.get('base')}' does not allow snapshot isolation "
                f"(snapshot_isolation_state={estado}). Enable it on the server "
                "(ALTER DATABASE ... SET ALLOW_SNAPSHOT_ISOLATION ON) or remove "
                "'isolamento' from the connection."
            ),
            status=409,
        )
    cur.execute(_SQL_ISOLAMENTO[isolamento])


def _primeiro_valor(linha):
    """The value of the single column, with the connection in dict (`as_dict=True`) or tuple mode."""
    if linha is None:
        return None
    if isinstance(linha, dict):
        return next(iter(linha.values()), None)
    return linha[0]


def _conectar(
    config: dict,
    senha: str | None,
    query_timeout: int | None = None,
    autocommit: bool = True,
    as_dict: bool = True,
):
    import pytds

    cfg = get_settings()
    # login_timeout = opening the connection (always short). timeout = the QUERY: in EXECUTION
    # (full data, large jobs) it uses the (long) query timeout; for test/preview, the short one.
    # autocommit=False (write) -> staging + final statement in a single transaction (atomic
    # rollback); the default True preserves test/sample/browse behaviour.
    conn = pytds.connect(
        dsn=config.get("host"),  # dsn = host/server (server= is deprecated in pytds)
        port=int(config.get("porta", 1433)),
        database=config.get("base"),
        user=config.get("usuario"),
        password=senha,
        login_timeout=cfg.conexao_teste_timeout_segundos,
        timeout=query_timeout if query_timeout is not None else cfg.conexao_teste_timeout_segundos,
        as_dict=as_dict,
        autocommit=autocommit,
        # TDS packet of 32767 bytes (default is 4096): 8x fewer packets, and with the columnar direct
        # read (`colunar.acelerar_leitura`) fewer values fall on a packet boundary and take the slow
        # path. Measured on 30k rows, ABAB x3, identical hash: CPU down ~5 %. Without the direct read
        # the gain was ~3 %.
        blocksize=32767,
    )
    # Applies per SESSION, so it lives here: the pooled connection is reused across extractions.
    try:
        _preparar_sessao(conn, config, autocommit)
    except Exception:
        conn.close()
        raise
    return conn


def _consultar(config: dict, senha: str | None, sql: str, params: tuple = ()) -> list[dict]:
    conn = _conectar(config, senha)
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _consultar_com_colunas(
    config: dict,
    senha: str | None,
    sql: str,
    params: tuple = (),
    query_timeout: int | None = None,
    usar_pool: bool = False,
) -> tuple[list[str], list[dict]]:
    """Like `_consultar`, but also returns the columns (from `cur.description`) EVEN with 0 rows.
    `usar_pool=True` (execution only) reuses the worker pool connection; otherwise creates and closes it."""
    with sqlserver_conn(config, lambda: _conectar(config, senha, query_timeout), usar_pool) as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        colunas = [d[0] for d in cur.description] if cur.description else []
        # The connection is `as_dict=True`: each row is already a fresh dict, copying it was pure allocation.
        return colunas, cur.fetchall()


def _amostra_serializada(
    config: dict,
    senha: str | None,
    sql: str,
    params: tuple,
    query_timeout: int,
    usar_pool: bool,
    truncar: int | None,
) -> tuple[list[str], list[dict]]:
    """Query + per-value `serial`, ALL inside the thread. The conversion used to run on the event
    loop, after `to_thread`: with hundreds of thousands of rows the loop stalled for tens of seconds
    and every node running alongside (publication, SQL, another source) waited on it (measured on a
    large replay)."""
    colunas, rows = _consultar_com_colunas(config, senha, sql, params, query_timeout, usar_pool)
    if truncar is not None:
        rows = rows[:truncar]
    return colunas, [{k: serial(v) for k, v in r.items()} for r in rows]


def _teto_de_processos(cfg) -> int:
    """Process ceiling for a sliced extraction: the configured one, capped by the CPUs the
    container can really use. Asking for more processes than CPUs does not speed up decoding (it is
    CPU-bound) and only multiplies the scan on the server (measured: 6 processes on 4 CPUs saturate
    at 1.97 s versus 1.90 s with 4).

    The SAME order as `duck.orcamento`: cgroup quota (`cpu.max`, a TIME ceiling, what container
    `resources.limits` writes) first, affinity (`cpuset`) second. Affinity alone would let a pod
    limited to 2 CPUs on a 32-core node open 32 decode processes."""
    from ...duck import cpus_afinidade, cpus_cgroup

    cpus = cpus_cgroup()
    if cpus is None:
        cpus = cpus_afinidade()
    visiveis = int(cpus) if cpus is not None else 4
    return max(1, min(int(cfg.extracao_processos_max), visiveis))


def _extrair_tabela_sync(
    config: dict,
    senha: str | None,
    sql: str,
    params: tuple,
    query_timeout: int,
    usar_pool: bool,
) -> Extracao:
    """COMPLETE columnar read (`conexoes.colunar`): batch by batch from the cursor into Arrow. Column
    names the columnar path does not accept fall back to the classic path, with the SAME cursor (no
    re-query).

    Rows as TUPLES (`as_dict=False`): the driver stops building a dict per row, which the columnar
    path would only take apart again. Name lookup keeps the semantics of the `pytds`
    `dict_row_strategy` (an empty name becomes the index and, with a repeated name, the LAST column
    wins), resolved once in `posicao`, not per row."""
    with sqlserver_conn(
        config,
        lambda: _conectar(config, senha, query_timeout, as_dict=False),
        usar_pool,
        variante="tupla",
    ) as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        colunas = [d[0] for d in cur.description] if cur.description else []
        nomes = [(n or str(i)) for i, n in enumerate(colunas)]
        cols_ok = colunar.colunas_validas(colunas)
        if not colunar.nomes_colunares(cols_ok):
            linhas = [{k: serial(v) for k, v in zip(nomes, t, strict=True)} for t in cur.fetchall()]
            return Extracao(colunas, linhas=linhas)
        posicao = {n: i for i, n in enumerate(nomes)}  # repeated: the last one wins, as in the dict
        indices = [posicao[c] for c in cols_ok]
        colunar.acelerar_leitura(cur)
        lotes = []
        while lote := cur.fetchmany(colunar.LOTE_LINHAS):
            lotes.append(colunar.lote_tuplas_para_arrow(lote, indices, cols_ok))
        if not lotes:
            return Extracao(colunas, linhas=[])
        return Extracao(colunas, tabela=colunar.tabela_de_lotes(lotes, cols_ok))


#: Queue of full extractions, per (loop, ceiling). See `Settings.extracao_concorrencia_max`.
_VAGAS_EXTRACAO: dict[tuple[int, int], asyncio.Semaphore] = {}


def _vaga_de_extracao(teto: int) -> asyncio.Semaphore:
    chave = (id(asyncio.get_running_loop()), teto)
    sem = _VAGAS_EXTRACAO.get(chave)
    if sem is None:
        sem = _VAGAS_EXTRACAO[chave] = asyncio.Semaphore(max(1, teto))
    return sem


class ConectorSqlServer:
    async def testar(self, config: dict, senha: str | None) -> ResultadoTeste:
        def _t():
            _conectar(config, senha).close()

        try:
            await asyncio.to_thread(_t)
            return ResultadoTeste(ok=True)
        except Exception as exc:  # noqa: BLE001 — sanitised cause
            return ResultadoTeste(ok=False, causa=_causa(exc))

    async def schemas_tabelas(self, config: dict, senha: str | None) -> list[dict]:
        rows = await asyncio.to_thread(
            _consultar,
            config,
            senha,
            "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_TYPE IN ('BASE TABLE','VIEW') ORDER BY TABLE_SCHEMA, TABLE_NAME",
        )
        schemas: dict[str, list[str]] = {}
        for r in rows:
            schemas.setdefault(r["TABLE_SCHEMA"], []).append(r["TABLE_NAME"])
        return [{"schema": s, "tabelas": t} for s, t in schemas.items()]

    async def colunas(
        self, config: dict, senha: str | None, schema: str, tabela: str
    ) -> list[dict]:
        rows = await asyncio.to_thread(
            _consultar,
            config,
            senha,
            "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            (schema, tabela),
        )
        return [{"nome": r["COLUMN_NAME"], "tipo": r["DATA_TYPE"]} for r in rows]

    async def amostra(
        self,
        config: dict,
        senha: str | None,
        sql: str,
        limite: int | None,
        params: dict | None = None,
    ) -> tuple[list[str], list[dict]]:
        sql_b, vals = vincular(sql, params, PYFORMAT)  # :nome -> %s + positional bind (pytds)
        # Execution (limite None) = long query, large query timeout; preview/sample = medium
        # (a real query, not just "connection alive"; the 5s connection-test timeout blows up on heavy sources).
        cfg = get_settings()
        qt_exec = cfg.conexao_query_timeout_segundos
        qt_amostra = cfg.conexao_amostra_timeout_segundos
        qt = qt_exec if limite is None else qt_amostra
        usar_pool = cfg.pool_fontes and limite is None  # pool only for execution reads
        # A CTE (WITH ...) cannot be wrapped in a subquery (SELECT * FROM (WITH...) is invalid
        # T-SQL). In execution (no TOP) the wrapper is unnecessary, so the raw SQL runs.
        # In a preview with a CTE, run raw and truncate in Python (TOP cannot be injected without parsing the CTE).
        cte = _e_cte(sql_b)
        if limite is None:
            query = _com_recompile(sql_b)
        elif cte:
            query = sql_b
        else:
            query = f"SELECT TOP {int(limite)} * FROM ({sql_b}) AS _sub"
        return await asyncio.to_thread(
            _amostra_serializada,
            config,
            senha,
            query,
            tuple(vals),
            qt,
            usar_pool,
            int(limite) if cte and limite is not None else None,
        )

    async def extrair_tabela(
        self,
        config: dict,
        senha: str | None,
        sql: str,
        params: dict | None = None,
        *,
        paralela: dict | None = None,
    ) -> Extracao:
        """COMPLETE columnar read for execution (see `conexoes.colunar`). Same SQL, same bind, same
        timeout and same pool as `amostra` with `limite=None`; what changes is the path of the data
        between the cursor and DuckDB. Data the columnar path cannot reproduce (`ForaDoColunar`, rare
        on SQL Server: NaN is not storable) falls back to `amostra`: it re-queries, and the result is
        the usual one."""
        cfg = get_settings()
        # Without the setting, the ceiling is the number of activities: the limit that applied BEFORE
        # the source took the DuckDB slot only after extraction (the slot, 8 per process, used to
        # limit in-flight extractions). With no ceiling at all, and activity slots beyond the thread
        # count, dozens of whole Arrow tables (hundreds of MB each) could wait for a DuckDB slot at
        # the same time, outside any budget.
        teto = getattr(cfg, "extracao_concorrencia_max", None) or getattr(
            cfg, "worker_max_atividades", 0
        )
        if not teto:
            return await self._extrair_tabela(config, senha, sql, params, paralela=paralela)
        async with _vaga_de_extracao(int(teto)):
            return await self._extrair_tabela(config, senha, sql, params, paralela=paralela)

    async def _extrair_tabela(
        self,
        config: dict,
        senha: str | None,
        sql: str,
        params: dict | None = None,
        *,
        paralela: dict | None = None,
    ) -> Extracao:
        sql_b, vals = vincular(sql, params, PYFORMAT)
        cfg = get_settings()
        # The ceiling is only read when the node asks for slicing: the usual path does not depend on it.
        plano = processos.ler_plano(paralela, _teto_de_processos(cfg)) if paralela else None
        if plano is not None and processos.particionavel(sql_b):
            try:
                return await asyncio.to_thread(
                    processos.extrair_fatiado,
                    config,
                    senha,
                    sql_b,
                    list(vals),
                    cfg.conexao_query_timeout_segundos,
                    plano,
                )
            except processos.ForaDoParalelo:
                pass  # the cut cannot be sliced: continue through the single-process path
        if getattr(cfg, "extracao_em_processo", False) and (teto := _teto_de_processos(cfg)) >= 2:
            # The whole cut in one pool process, unsliced: the same query (one scan on the server)
            # with decoding outside the worker GIL. `None` = not columnar -> thread.
            ext = await asyncio.to_thread(
                processos.extrair_em_processo,
                config,
                senha,
                _com_recompile(sql_b),
                list(vals),
                cfg.conexao_query_timeout_segundos,
                teto,
            )
            if ext is not None:
                return ext
        try:
            return await asyncio.to_thread(
                _extrair_tabela_sync,
                config,
                senha,
                _com_recompile(sql_b),
                tuple(vals),
                cfg.conexao_query_timeout_segundos,
                cfg.pool_fontes,
            )
        except colunar.ForaDoColunar:
            colunas, linhas = await self.amostra(config, senha, sql, None, params)
            return Extracao(colunas, linhas=linhas)

    async def escrever_tabela(
        self,
        config: dict,
        senha: str | None,
        *,
        schema: str,
        tabela: str,
        colunas_destino: list[str],
        linhas: list[dict],
        estrategia: str,
        chave: list[str] | None,
        lote_tamanho: int,
    ) -> int:
        afetados, _ = await asyncio.to_thread(
            _escrever_tabela_sync,
            config,
            senha,
            schema,
            tabela,
            colunas_destino,
            linhas,
            estrategia,
            chave,
            lote_tamanho,
        )
        return afetados

    async def escrever_tabela_no_escopo(
        self,
        config: dict,
        senha: str | None,
        *,
        schema: str,
        tabela: str,
        colunas_destino: list[str],
        linhas: list[dict],
        estrategia: str,
        chave: list[str] | None,
        lote_tamanho: int,
        escopo: dict[str, str],
        params: dict,
    ) -> dict:
        # Refuse BEFORE opening a connection: a malformed scope or a parameter without a value must
        # never reach a DELETE, neither `WHERE col = NULL` (deletes nothing and writes on top) nor no WHERE.
        conferir_escopo(escopo, estrategia, colunas_destino)
        valores = valores_do_escopo(escopo, params)
        afetados, apagados = await asyncio.to_thread(
            _escrever_tabela_sync,
            config,
            senha,
            schema,
            tabela,
            colunas_destino,
            linhas,
            estrategia,
            chave,
            lote_tamanho,
            escopo,
            valores,
        )
        return {"linhas_afetadas": afetados, "linhas_apagadas": apagados}

    async def executar_dml(
        self,
        config: dict,
        senha: str | None,
        sql: str,
        params: dict | None,
        entrada: tuple[list[str], list[dict]] | None,
    ) -> int:
        return await asyncio.to_thread(_executar_dml_sync, config, senha, sql, params, entrada)


def _limpar_escopo_sync(cur, schema, tabela, escopo, valores) -> int:
    """Checks staging against the scope and deletes the scope on the target, on the write's cursor
    (and transaction). Runs AFTER `#stage` is loaded: the HOLDLOCK lock lasts only DELETE + write."""
    sql_f, vals_f = vincular(montar_sql_fora_do_escopo_mssql(escopo), valores, PYFORMAT)
    cur.execute(sql_f, tuple(vals_f))
    fora = _escalar(cur)
    if fora:
        raise ErroAplicacao(
            codigo="escrita_fora_do_escopo",
            mensagem=(
                f"{fora} input row(s) outside the scope that the write clears "
                f"({', '.join(escopo)}). Writing them would leave rows that no run of this "
                "scope deletes."
            ),
            status=400,
        )
    sql_d, vals_d = vincular(montar_sql_limpeza_mssql(schema, tabela, escopo), valores, PYFORMAT)
    cur.execute(sql_d, tuple(vals_d))
    cur.execute("SELECT @@ROWCOUNT AS n")
    return _escalar(cur)


def _escrever_tabela_sync(
    config,
    senha,
    schema,
    tabela,
    colunas_destino,
    linhas,
    estrategia,
    chave,
    lote_tamanho,
    escopo=None,
    valores_escopo=None,
) -> tuple[int, int | None]:
    """(rows written, rows deleted from the scope; None without `limpar_escopo`)."""
    qt = get_settings().conexao_query_timeout_segundos
    alvo = f"{_qb(schema)}.{_qb(tabela)}"
    cols = ", ".join(_qb(c) for c in colunas_destino)
    phs = ", ".join(["%s"] * len(colunas_destino))
    registros = [tuple(lin.get(c) for c in colunas_destino) for lin in linhas]
    lote = max(1, int(lote_tamanho))
    conn = _conectar(config, senha, query_timeout=qt, autocommit=False)
    try:
        cur = conn.cursor()
        # #stage (session temp table) inherits the target TYPES; MERGE/#temp require the SAME connection/cursor.
        cur.execute(f"SELECT TOP 0 {cols} INTO #stage FROM {alvo}")
        ins = f"INSERT INTO #stage ({cols}) VALUES ({phs})"
        for i in range(0, len(registros), lote):
            cur.executemany(ins, registros[i : i + lote])
        apagados = None
        if escopo is not None:
            apagados = _limpar_escopo_sync(cur, schema, tabela, escopo, valores_escopo)
        if estrategia == "truncate_insert":
            cur.execute(f"TRUNCATE TABLE {alvo}")
        cur.execute(montar_sql_tabela_mssql(schema, tabela, colunas_destino, estrategia, chave))
        # @@ROWCOUNT = EXACT count of the last statement (includes MERGE); cur.rowcount may be -1
        # on a pytds MERGE. Read it immediately after the statement, before any other.
        cur.execute("SELECT @@ROWCOUNT AS n")
        afetados = _escalar(cur)
        conn.commit()  # ONE commit: scope clearing and the write persist together or not at all
        return afetados, apagados
    except ErroAplicacao:
        conn.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 — rollback; sanitised cause (no secret)
        conn.rollback()
        raise ErroAplicacao(
            codigo="escrita_falhou",
            mensagem="Failed to write to the external destination.",
            status=400,
        ) from exc
    finally:
        conn.close()


def _executar_dml_sync(config, senha, sql, params, entrada) -> int:
    qt = get_settings().conexao_query_timeout_segundos
    conn = _conectar(config, senha, query_timeout=qt, autocommit=False)
    try:
        cur = conn.cursor()
        if entrada is not None:
            cols, linhas = entrada
            coldef = ", ".join(f"{_qb(c)} nvarchar(max)" for c in cols)
            cur.execute(f"CREATE TABLE #entrada ({coldef})")
            if linhas:
                colq = ", ".join(_qb(c) for c in cols)
                phs = ", ".join(["%s"] * len(cols))
                ins = f"INSERT INTO #entrada ({colq}) VALUES ({phs})"
                regs = [
                    tuple(None if lin.get(c) is None else str(lin.get(c)) for c in cols)
                    for lin in linhas
                ]
                for i in range(0, len(regs), 1000):
                    cur.executemany(ins, regs[i : i + 1000])
            # In T-SQL the staging is a temp table (#entrada); the user writes `_entrada`, so we rewrite
            # it (code only, preserving literals/comments).
            sql = _reescrever_entrada(sql)
        sql_b, vals = vincular(sql, params, PYFORMAT)
        cur.execute(sql_b, tuple(vals))
        # @@ROWCOUNT: exact DML count (cur.rowcount may be -1 for some statements).
        cur.execute("SELECT @@ROWCOUNT AS n")
        afetados = _escalar(cur)
        conn.commit()
        return afetados
    except ErroAplicacao:
        conn.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 — rollback; sanitised cause (no secret)
        conn.rollback()
        raise ErroAplicacao(
            codigo="escrita_falhou",
            mensagem="Failed to run the DML on the destination.",
            status=400,
        ) from exc
    finally:
        conn.close()
