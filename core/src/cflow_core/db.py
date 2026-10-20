"""Data access through SQLAlchemy Core (no ORM) + asyncpg.

The runtime engine connects as cflow_app (never as the owner). The business write and the audit
INSERT share the SAME transaction; see cflow_core.audit.service.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from .config import get_settings
from .runtime import contexto_ssl

# Engine cached PER event loop. asyncpg/SQLAlchemy bind the engine to the loop it was born in;
# at normal runtime there is a single loop (a single engine). In tests, the per-test loop and
# the in-process Temporal worker (own thread/loop) each get THEIR engine, with no connections
# leaking between loops.
_engines: dict[int, AsyncEngine] = {}

#: Local queue of `trava_consultiva`, per (loop, key). See there.
_travas_locais: dict[tuple[int, str], asyncio.Lock] = {}
_TETO_TRAVAS_LOCAIS = 4096


def _chave_loop() -> int:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def get_engine() -> AsyncEngine:
    """Async runtime engine (cflow_app role), one per event loop."""
    chave = _chave_loop()
    eng = _engines.get(chave)
    if eng is None:
        s = get_settings()
        # Behind pgbouncer in transaction mode, a connection's prepared statement does not exist on
        # the next one: asyncpg needs the cache off, else "prepared statement does not exist".
        connect_args: dict = {"statement_cache_size": 0} if s.db_atras_de_pgbouncer else {}
        # Optional TLS: `database_tls` turns it on and the CA file (if any) verifies the chain.
        if s.database_tls:
            connect_args["ssl"] = contexto_ssl(s.database_ca_file)
        if s.database_schema:
            connect_args["server_settings"] = {"search_path": s.database_schema}
        eng = create_async_engine(
            s.database_url,
            pool_pre_ping=True,
            future=True,
            pool_size=int(s.db_pool_size),
            max_overflow=int(s.db_max_overflow),
            connect_args=connect_args,
        )
        _engines[chave] = eng
    return eng


async def dispor_engine_do_loop() -> None:
    """Disposes of (and removes from the cache) the current loop's engine. Used between tests
    (per-test loop); does not affect engines of other loops (e.g. in-process worker)."""
    eng = _engines.pop(_chave_loop(), None)
    if eng is not None:
        await eng.dispose()


def _limpar_engines() -> None:
    """Empties the cache (no dispose). Compatible with old get_engine.cache_clear() calls."""
    _engines.clear()


get_engine.cache_clear = _limpar_engines  # type: ignore[attr-defined]


@asynccontextmanager
async def transacao() -> AsyncIterator[AsyncConnection]:
    """Opens a connection and a transaction. Commit on success, rollback on exception.

    Every auditable write runs inside this context so that the business write and the audit
    record are atomic.
    """
    engine = get_engine()
    async with engine.begin() as conn:  # begin() = transaction; automatic rollback on error
        yield conn


@asynccontextmanager
async def autocommit() -> AsyncIterator[AsyncConnection]:
    """Connection WITHOUT an explicit transaction: each statement is its own transaction in Postgres.

    For single-statement state writes (or reads), such as the start mark, reuse, completion and
    failure of a node in `execucao_nos`, where BEGIN and COMMIT protect no atomicity and cost two
    event-loop round trips each. **Nothing that writes audit uses this**: business write + trail
    stay in `transacao()`."""
    async with get_engine().connect() as conn:
        yield await conn.execution_options(isolation_level="AUTOCOMMIT")


@asynccontextmanager
async def trava_consultiva(chave: str) -> AsyncIterator[AsyncConnection]:
    """Cross-process critical section, through Postgres: `pg_advisory_xact_lock(hashtext(key))`.

    For read-merge-write on a shared object (e.g. the lake's `<dataset>/_atual.json` index) whose
    writers are ALL workers of this platform using the same database. Simpler and more portable
    (disk and GCS) than a per-generation CAS on the object. The lock belongs to the transaction:
    released on leaving the `with`, including on exception. Whoever needs the database inside uses
    the returned connection; whoever opens its own `transacao()` does not deadlock (distinct
    connection) but is not protected by anyone's lock other than this key."""
    from sqlalchemy import text

    # LOCAL queue first (per process and loop), then Postgres'. Without it, each coroutine
    # waiting for the lock holds a pool connection parked on `pg_advisory_xact_lock`; with several
    # publications of the same dataset in flight, that exhausts the pool and other nodes fail on
    # checkout timeout. This way at most ONE connection per key and per process waits in the
    # database; across processes, the Postgres lock still applies.
    local = _travas_locais.setdefault((_chave_loop(), chave), asyncio.Lock())
    async with local:
        async with transacao() as conn:
            await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:c))"), {"c": chave})
            yield conn
    if len(_travas_locais) > _TETO_TRAVAS_LOCAIS:
        # Prune what nobody uses: the dict must not grow unbounded in a long-lived worker (one key
        # per published dataset). `locked()` alone is not enough: right after a `release` with
        # someone queued the lock is free but PROMISED; removing it would make the next caller
        # create another and bypass the local queue next to the one waiting.
        for k in [
            k
            for k, t in _travas_locais.items()
            if not t.locked() and not getattr(t, "_waiters", None)
        ]:
            _travas_locais.pop(k, None)


async def verificar_conexao() -> bool:
    """Database connectivity health check. True if SELECT 1 answers."""
    from sqlalchemy import text

    engine = get_engine()
    async with engine.connect() as conn:
        resultado = await conn.execute(text("SELECT 1"))
        return resultado.scalar_one() == 1
