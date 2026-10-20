"""SOURCE extraction in PROCESSES, when the node declares which column to slice by.

**Why processes and not threads.** TDS decoding is pure Python (`pytds`, MIT; the compiled drivers
that exist carry proprietary or LGPL licences) and does not release the GIL. Measured on a
production-like host against a table of ~29k rows x 51 columns (~1.5 M values), with waits read from
`sys.dm_exec_session_wait_stats` of the session ITSELF:

| path | wall time |
| --- | --- |
| `execute` (server producing) | 0.004 s |
| `fetchall` (client decoding) | 5.82 s |
| session `ASYNC_NETWORK_IO` | 5.32 s over 30 waits |
| threads = 1 / 4 / 8 | 6.02 / 6.15 / 6.16 s |
| processes = 1 / 2 / 4 / 6 | 6.65 / 3.39 / **1.90** / 1.97 s |

The server delivers in 4 ms and spends 5.3 s IDLE waiting for the client to consume: the bottleneck
is decoding, not SQL Server I/O or the network (RTT 0.160 ms). Threads do not move the number: it is
the GIL. Processes scale up to the CPU count (the test host has 4; at 6 it saturates).

**Slicing is opt-in and needs a STABLE column.** The table is read with `(nolock)`: slicing by
`CHECKSUM(*)` would let a row changed between two slices switch bucket and come out duplicated or
missing, a silent data error. By an immutable key (the PK) a row always falls in the same bucket,
and the slices stay disjoint and exhaustive. That is why the column is DECLARED on the node and
never guessed: the code cannot know which column the source never updates.

**The cost is on the server.** N slices = N scans of the same cut. Worth it when the server delivers
quickly (measured: < 1 s cold for typical cuts) and the client is the bottleneck.
"""

from __future__ import annotations

import contextlib
import io
import re
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from multiprocessing import get_context

from . import colunar
from .colunar import Extracao

#: Column name that becomes a predicate without dynamic quoting. Same yardstick as the
#: escrita_sql identifiers: the value comes from the versioned definition, but concatenating text
#: into a WHERE still requires the guard.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: What cannot be wrapped in a subquery, or whose semantics the wrapping would change.
_CTE = re.compile(r"^\s*WITH\s", re.IGNORECASE)
_PROIBIDO = re.compile(
    r"\b(ORDER\s+BY|OFFSET|FOR\s+XML|FOR\s+JSON|INTO|OPTION\s*\()", re.IGNORECASE
)
_TOP = re.compile(r"^\s*SELECT\s+(DISTINCT\s+)?TOP\b", re.IGNORECASE)
_ENTRADA = re.compile(r"\b_entrada\b")
_COMENTARIO = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


class ForaDoParalelo(Exception):
    """The cut cannot be sliced: the caller proceeds through the single-process path."""


@dataclass(frozen=True)
class Plano:
    """What the node declared. `processos` is already capped by the worker ceiling; `teto` is the
    size of the process pool, which is a single one shared by all extractions (see `_pool`)."""

    coluna: str
    processos: int
    teto: int


def ler_plano(cfg: dict | None, teto: int) -> Plano | None:
    """Reads `config.extracao_paralela` of the node. Absent -> `None` (usual behaviour).

    Shape: `{"coluna": "<identifier>", "processos": <2..teto>}`. `processos` is optional
    (default: the ceiling). A malformed config raises: accepting it silently would produce a node
    that looks configured and runs sequentially, which is the defect this validation exists to prevent.
    """
    if not cfg:
        return None
    if not isinstance(cfg, dict):
        raise ForaDoParalelo("extracao_paralela must be an object")
    coluna = cfg.get("coluna")
    if not isinstance(coluna, str) or not _IDENT.match(coluna):
        raise ForaDoParalelo("extracao_paralela.coluna must be a simple SQL identifier")
    n = cfg.get("processos", teto)
    if not isinstance(n, int) or isinstance(n, bool) or n < 2:
        raise ForaDoParalelo("extracao_paralela.processos must be an integer >= 2")
    if min(n, teto) < 2:
        # Valid config, but the environment has no CPU to parallelise (1-CPU quota): a single slice
        # would be the sequential path plus the cost of the child process and IPC. Not a node error.
        return None
    return Plano(coluna=coluna, processos=min(n, teto), teto=teto)


def particionavel(sql: str) -> bool:
    """Does the SQL accept the wrapper `SELECT * FROM (<sql>) AS _fatia WHERE ...`?

    Refuses CTEs (`SELECT * FROM (WITH ...)` is invalid T-SQL, same reason as `_e_cte` in
    `amostra`), `ORDER BY`/`OFFSET`/`TOP` (the wrapper changes what the query means: ordering or
    limiting INSIDE a slice is not ordering or limiting the whole), `_entrada` (a session staging
    table that does not exist on the child process connection) and `OPTION (...)` (the slice adds
    its own, and an `OPTION` inside a subquery is invalid T-SQL)."""
    limpo = _COMENTARIO.sub(" ", sql)
    if _CTE.match(limpo) or _TOP.match(limpo):
        return False
    return not (_PROIBIDO.search(limpo) or _ENTRADA.search(limpo))


def sql_da_fatia(sql: str, coluna: str, n: int, k: int) -> str:
    """The SQL of slice `k` of `n`.

    Three pitfalls, each breaking in a different way:

    1. **No `%` in the text.** `pytds` interpolates the query `%`-style, so a literal `%` becomes a
       format specifier and execution dies with "not enough arguments for format string", the SAME
       collision `LIKE` already had. `vincular` escapes what comes from the user, but this
       predicate is built AFTER it. `c - (c / n) * n` is the remainder by definition in T-SQL
       (integer division truncates toward zero, like `%`), and has no such character.
    2. **Modulo before `ABS`.** `CHECKSUM` can return -2,147,483,648, and `ABS` of that value
       overflows with "Arithmetic overflow", on whichever row produced it. The remainder comes first
       and stays within -(n-1)..(n-1), where `ABS` is safe.
    3. **`OPTION (RECOMPILE)`.** Each slice is a DIFFERENT SQL TEXT (`= 0`, `= 1`, ...), with its
       own cached plan, compiled for the first operation that used it and reused for the others.
       Measured with `sys.dm_exec_query_stats`: slices compiled for a small input and reused for an
       input 7x larger cost hundreds of thousands of logical reads and tens of seconds of CPU PER
       EXECUTION, against ~6 thousand reads and 0.1 s for slices that compiled well. Recompiling
       costs milliseconds, against extractions that take seconds. The single-process path suffers
       from the same plan reuse, so it recompiles too (see `sqlserver._com_recompile`).

    It is still a deterministic function of the row, so the slices stay disjoint and exhaustive:
    each row falls into exactly one bucket."""
    if not _IDENT.match(coluna):
        raise ForaDoParalelo(f"invalid slice column: {coluna!r}")
    n, k = int(n), int(k)
    c = f"CHECKSUM(_fatia.[{coluna}])"
    return f"SELECT * FROM ({sql}) AS _fatia WHERE ABS({c} - ({c} / {n}) * {n}) = {k} OPTION (RECOMPILE)"


# ---------------------------------------------------------------- child process


def _extrair_fatia(pedido: tuple) -> bytes | None:
    """Runs IN THE CHILD. Returns the slice as Arrow IPC, or `None` when the data is not columnar
    (`ForaDoColunar`), in which case the parent redoes the whole extraction through the classic path.

    Opens its own connection: the worker pool belongs to the parent process and does not cross
    `fork`/`spawn`.
    """
    import pyarrow as pa

    from .conectores.sqlserver import _conectar

    config, senha, sql, vals, timeout = pedido
    conn = _conectar(config, senha, timeout, as_dict=False)
    try:
        cur = conn.cursor()
        cur.execute(sql, tuple(vals))
        colunas = [d[0] for d in cur.description] if cur.description else []
        nomes = [(n or str(i)) for i, n in enumerate(colunas)]
        cols_ok = colunar.colunas_validas(colunas)
        if not colunar.nomes_colunares(cols_ok):
            return None
        posicao = {n: i for i, n in enumerate(nomes)}
        indices = [posicao[c] for c in cols_ok]
        colunar.acelerar_leitura(cur)
        lotes = []
        try:
            while lote := cur.fetchmany(colunar.LOTE_LINHAS):
                lotes.append(colunar.lote_tuplas_para_arrow(lote, indices, cols_ok))
        except colunar.ForaDoColunar:
            return None
        tabela = (
            colunar.tabela_de_lotes(lotes, cols_ok)
            if lotes
            else pa.table([pa.array([], type=pa.string()) for _ in cols_ok], names=cols_ok)
        )
    finally:
        conn.close()
    buf = io.BytesIO()
    with pa.ipc.new_stream(buf, tabela.schema) as escritor:
        escritor.write_table(tabela)
    return buf.getvalue()


#: CHILD PROCESS connections, per destination and credential. The whole-cut extraction
#: (`_extrair_inteira`) runs many times per run (dozens of sources in a large load), and opening a
#: new connection over a slow link costs a TDS login each time. Lives only in the child: the worker
#: pool (parent) does not cross `spawn`.
_CONEXOES_FILHO: dict[tuple, object] = {}


def _conexao_do_filho(config: dict, senha: str | None, timeout: int, *, nova: bool = False):
    from .conectores.sqlserver import _conectar

    chave = (
        config.get("host"),
        str(config.get("porta", 1433)),
        config.get("base"),
        config.get("usuario"),
        config.get("isolamento"),
        hash(senha),
        timeout,
    )
    conn = None if nova else _CONEXOES_FILHO.get(chave)
    if conn is None:
        velha = _CONEXOES_FILHO.pop(chave, None)
        if velha is not None:
            with contextlib.suppress(Exception):
                velha.close()
        conn = _CONEXOES_FILHO[chave] = _conectar(config, senha, timeout, as_dict=False)
    return conn


def _eh_conexao_perdida(exc: BaseException) -> bool:
    """Failure that a NEW connection fixes: a dropped socket (a network device or the server closed
    the idle connection). Query timeouts and SQL errors are excluded: repeating would double the cost
    without changing the outcome."""
    return isinstance(exc, (ConnectionError, EOFError, BrokenPipeError)) or type(exc).__name__ in {
        "ClosedConnectionError",
        "OperationalError",
    }


def _extrair_inteira(pedido: tuple) -> tuple:
    """Runs IN THE CHILD. Never lets an exception cross `pickle`: some `pytds` exceptions (and
    `ErroAplicacao`) cannot be rebuilt on the other side, and the parent receives
    `BrokenProcessPool` instead of the error, so the whole pool stops being usable. The error comes
    back as `("erro", text)`."""
    try:
        return _extrair_inteira_no_filho(pedido)
    except BaseException as exc:  # noqa: BLE001 — handed back to the parent as data, never swallowed
        _CONEXOES_FILHO.clear()  # a connection with a half-read result does not go back to the cache
        return ("erro", f"{type(exc).__name__}: {exc}"[:2000])


def _extrair_inteira_no_filho(pedido: tuple) -> tuple[list, bytes | None]:
    """Runs IN THE CHILD: the WHOLE cut, unsliced: same SQL, same bind, same session
    (`_conectar` sets up the isolation) and therefore the same query as the in-thread path.

    Returns `(cursor columns, Arrow IPC)`. `None` in place of the bytes = the column names or the
    data are not columnar: the parent redoes it through the classic path, which has the dict
    fallback. `b""` = zero rows (the parent returns `linhas=[]`, like the in-thread path).

    A reused connection that lost its socket on `execute` is replaced ONCE: it is a SELECT, repeating
    has no side effect, and the most common failure there is an idle connection dropped by the
    network or the server. Any other failure propagates (a repeated query timeout would double the
    time and the scan)."""
    import pyarrow as pa

    config, senha, sql, vals, timeout = pedido
    for tentativa in (0, 1):
        conn = _conexao_do_filho(config, senha, timeout, nova=tentativa == 1)
        try:
            cur = conn.cursor()
            cur.execute(sql, tuple(vals))
            break
        except Exception as exc:
            if tentativa == 1 or not _eh_conexao_perdida(exc):
                raise
    colunas = [d[0] for d in cur.description] if cur.description else []
    nomes = [(n or str(i)) for i, n in enumerate(colunas)]
    cols_ok = colunar.colunas_validas(colunas)
    if not colunar.nomes_colunares(cols_ok):
        cur.fetchall()  # consume the result so the connection returns clean for the next extraction
        return colunas, None
    posicao = {n: i for i, n in enumerate(nomes)}
    indices = [posicao[c] for c in cols_ok]
    colunar.acelerar_leitura(cur)
    lotes = []
    try:
        while lote := cur.fetchmany(colunar.LOTE_LINHAS):
            lotes.append(colunar.lote_tuplas_para_arrow(lote, indices, cols_ok))
    except colunar.ForaDoColunar:
        cur.fetchall()
        return colunas, None
    if not lotes:
        return colunas, b""
    tabela = colunar.tabela_de_lotes(lotes, cols_ok)
    del lotes
    buf = io.BytesIO()
    with pa.ipc.new_stream(buf, tabela.schema) as escritor:
        escritor.write_table(tabela)
    return colunas, buf.getvalue()


def extrair_em_processo(
    config: dict, senha: str | None, sql: str, vals: list, timeout: int, teto: int
) -> Extracao | None:
    """The whole cut in one process of the pool (unsliced). `None` = not columnar: the caller
    proceeds through the in-thread path, which re-queries and falls back to dicts, so the result is
    the usual one.

    Why: TDS decoding holds the GIL. During a load, several large extractions run at the same time
    in the SAME process (measured: ~47 % of what a lone process decodes). In processes, each has its
    own GIL. The query is the same, a single scan, without the N-fold server cost of slicing.

    On a high-latency link the bottleneck (decode or bandwidth) may not be separated yet.
    Opt-in until measured."""
    import pyarrow as pa

    pool = _pool(teto)
    try:
        colunas, ipc = pool.submit(
            _extrair_inteira, (config, senha, sql, list(vals), timeout)
        ).result()
    except BrokenProcessPool:
        # A child died (typically OOM-killed: it is the largest process in the cgroup). The broken
        # pool would refuse every extraction until the worker restarts; replace IT and let this one
        # fall back to the thread path.
        _descartar(pool)
        return None
    if colunas == "erro":
        # Query failure in the child, as text. Raised as a plain exception (not `ErroAplicacao`):
        # same outcome as the in-thread path, which the orchestrator retries as an infrastructure failure.
        raise RuntimeError(f"in-process extraction failed: {ipc}")
    if ipc is None:
        return None
    if ipc == b"":
        return Extracao(colunas, linhas=[])
    # Straight over the bytes: `BytesIO` would force Arrow to copy into its own buffers.
    tabela = pa.ipc.open_stream(pa.py_buffer(ipc)).read_all()
    return Extracao(colunas, tabela=tabela)


# ---------------------------------------------------------------- parent process

_POOL: ProcessPoolExecutor | None = None
_TRAVA = threading.Lock()


def _pool(teto: int) -> ProcessPoolExecutor:
    """The worker process pool: created ONCE, under a lock, and NEVER replaced.

    **An earlier version hung the worker (measured).** It was a lock-free module singleton that
    recreated itself whenever someone asked for more processes than the pool had. The worker runs
    several nodes in threads, and with 5 simultaneous extractions two threads saw an empty `_POOL`
    at the same time and created ONE POOL EACH: 8 children for a ceiling of 4. The pool that lost the
    race was left unreferenced; its children finished the query and got stuck handing the result to
    a manager nobody read anymore. Seven minutes stalled with idle child CPUs, against 120 s on the
    sequential path. Swapping the pool for a bigger one has the same defect: the old pool may be in
    the middle of another thread's `map`.

    Hence: size = `teto` (fixed per worker process), created on the first sliced extraction and
    reused by all. Each extraction asks for `plano.processos` slices (<= ceiling); asking for fewer
    does not need a smaller pool.

    `spawn`, never `fork`: the worker is multi-threaded and `fork` of a threaded process inherits a
    lock held by a thread that does not exist on the other side. The `spawn` cost is paid once per
    child process, not per extraction."""
    global _POOL
    with _TRAVA:
        if _POOL is None:
            _POOL = ProcessPoolExecutor(max_workers=teto, mp_context=get_context("spawn"))
        return _POOL


def _descartar(pool: ProcessPoolExecutor) -> None:
    """Discards `pool` if it is still the current one, under the SAME lock as `_pool`, so two threads
    that saw the same pool break do not create two new pools (the defect `_pool` documents). A pool
    that has already been replaced is left alone: its late error does not take down the healthy one."""
    global _POOL
    with _TRAVA:
        if _POOL is pool:
            _POOL = None
    pool.shutdown(wait=False, cancel_futures=True)


def encerrar() -> None:
    """Shuts the pool down (worker shutdown and tests)."""
    global _POOL
    with _TRAVA:
        if _POOL is not None:
            _POOL.shutdown(wait=True)
            _POOL = None


def extrair_fatiado(
    config: dict, senha: str | None, sql: str, vals: list, timeout: int, plano: Plano
) -> Extracao:
    """Reads the cut in `plano.processos` slices and returns ONE Arrow table.

    Slices are concatenated in index order (0..n-1), not in the order they finish: the node hash is
    order-independent (`engine._hash_conteudo` sums each row's hash), but a consumer aggregating
    DOUBLE is not, and arrival order would change on every run, making that consumer's last bit
    non-reproducible. (It is NOT the same order as the single-process path.)

    A non-columnar slice brings down the whole slicing (`ForaDoParalelo`): rebuilding the classic
    path from half-converted data would produce text from two different recipes.
    """
    import pyarrow as pa

    n = plano.processos
    pedidos = [
        (config, senha, sql_da_fatia(sql, plano.coluna, n, k), list(vals), timeout)
        for k in range(n)
    ]
    pool = _pool(plano.teto)
    try:
        partes = list(pool.map(_extrair_fatia, pedidos))
    except BrokenProcessPool:
        _descartar(pool)
        raise ForaDoParalelo(
            "an extraction process died; falling back to the single-process path"
        ) from None
    if any(p is None for p in partes):
        raise ForaDoParalelo("slice with data the columnar path cannot reproduce")
    tabelas = [pa.ipc.open_stream(pa.py_buffer(p)).read_all() for p in partes]
    tabela = pa.concat_tables(tabelas)
    return Extracao(list(tabela.column_names), tabela=tabela)
