"""Per-process SOURCE connection pool (worker), OPT-IN via `CFLOW_POOL_FONTES` (off by default).

Motivation: in a fan-out (1 execution per operation) every source node reconnects, and the SQL
Server / gateway login is expensive. Reusing live connections within a short idle TTL cuts that
cost without leaking (they close by TTL/health-check) and keeps the secret only in worker memory for
the TTL.

Deliberately narrow scope: only EXECUTION READS (`amostra` with `limite is None`) use the pool.
Preview/test/browsing keep a fresh connection (interactive, short timeout, no reuse). Pool off: the
helpers just create and close the connection (identical to the previous behaviour).

Security: the secret lives inside the live connection for at most the idle TTL; opt-in and
worker-only. The plaintext secret is never stored outside the connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import threading
import time as _clock
from collections.abc import AsyncIterator, Callable, Iterator

from ..config import get_settings


def chave_conexao(config: dict) -> str:
    """Destination identity (host/port/database/user), grouping reusable connections. No secret."""
    base = (
        f"{config.get('host')}:{config.get('porta')}/{config.get('base')}@{config.get('usuario')}"
    )
    return hashlib.sha256(base.encode()).hexdigest()[:16]


# ===== SQL Server (synchronous pytds): simple thread-safe pool (list of free connections) =====
_sql_lock = threading.Lock()
_sql_livres: dict[str, list[tuple]] = {}  # key -> [(conn, last_use_monotonic)]


def _sql_vivo(conn) -> bool:
    """Cheap health check: SELECT 1. If it fails the connection died (server idle timeout etc.)."""
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchall()
        cur.close()
        return True
    except Exception:  # noqa: BLE001 — suspect connection: discard it
        return False


@contextlib.contextmanager
def sqlserver_conn(
    config: dict, criar: Callable[[], object], usar_pool: bool, variante: str = ""
) -> Iterator:
    """Lends a SQL Server connection. `usar_pool=False` creates and closes it (default). `True` reuses
    a live pooled connection (idle TTL + health check) or creates one, and returns it at the end. On
    a query error the connection does NOT go back to the pool (it may be dirty).

    `variante` keeps non-interchangeable connections apart in the pool: columnar extraction opens
    with TUPLE rows (`as_dict=False`), and lending one to a caller expecting dicts would break it."""
    if not usar_pool:
        conn = criar()
        try:
            yield conn
        finally:
            with contextlib.suppress(Exception):
                conn.close()
        return

    cfg = get_settings()
    ttl = cfg.pool_fontes_ttl_segundos
    k = chave_conexao(config) + (f"|{variante}" if variante else "")
    agora = _clock.monotonic()
    conn = None
    expiradas: list = []
    with _sql_lock:
        fila = _sql_livres.get(k, [])
        viva = [(c, ts) for c, ts in fila if agora - ts <= ttl]
        expiradas = [c for c, ts in fila if agora - ts > ttl]
        if viva:
            conn, _ = viva.pop()
        _sql_livres[k] = viva
    for c in expiradas:  # close the expired ones outside the lock
        with contextlib.suppress(Exception):
            c.close()
    if conn is not None and not _sql_vivo(conn):  # reused but dead: recreate
        with contextlib.suppress(Exception):
            conn.close()
        conn = None
    if conn is None:
        conn = criar()
    try:
        yield conn
    except Exception:
        with contextlib.suppress(Exception):
            conn.close()
        raise
    else:  # return to the pool (respecting the per-key cap)
        with _sql_lock:
            fila = _sql_livres.setdefault(k, [])
            if len(fila) < cfg.pool_fontes_max:
                fila.append((conn, _clock.monotonic()))
            else:
                with contextlib.suppress(Exception):
                    conn.close()


def fechar_sqlserver() -> None:
    """Closes all idle SQL Server connections (worker shutdown / tests)."""
    with _sql_lock:
        todas = [c for fila in _sql_livres.values() for c, _ in fila]
        _sql_livres.clear()
    for c in todas:
        with contextlib.suppress(Exception):
            c.close()


def _expirar_sqlserver_idle() -> int:
    """Closes SQL Server connections idle BEYOND the TTL (without waiting for the next checkout).
    Returns how many it closed. Synchronous: called by the reaper via to_thread (pytds close blocks)."""
    ttl = get_settings().pool_fontes_ttl_segundos
    agora = _clock.monotonic()
    fechar: list = []
    with _sql_lock:
        for k, fila in list(_sql_livres.items()):
            _sql_livres[k] = [(c, ts) for c, ts in fila if agora - ts <= ttl]
            fechar += [c for c, ts in fila if agora - ts > ttl]
    for c in fechar:
        with contextlib.suppress(Exception):
            c.close()
    return len(fechar)


async def reaper_loop() -> None:
    """Worker background task: every TTL, closes expired idle source connections so idle ones do NOT
    stay hanging (holding a database slot / the secret) during periods without executions.
    Only makes sense with CFLOW_POOL_FONTES on. (Postgres recycles idle connections by itself via
    asyncpg.Pool; this handles SQL Server, whose pool is manual.) Exits cleanly on task cancellation."""
    while True:
        await asyncio.sleep(max(5, get_settings().pool_fontes_ttl_segundos))
        with contextlib.suppress(Exception):
            await asyncio.to_thread(_expirar_sqlserver_idle)


# ===== Postgres (asyncpg): one asyncpg.Pool per (event loop, key) =====
_pg_pools: dict[tuple, object] = {}  # (id(loop), key) -> asyncpg.Pool
_pg_lock = asyncio.Lock()  # serialises creation (idempotent per key)


async def _pg_obter_pool(config: dict, senha: str | None):
    import asyncpg

    cfg = get_settings()
    chave = (id(asyncio.get_running_loop()), chave_conexao(config))
    pool = _pg_pools.get(chave)
    if pool is not None:
        return pool
    async with _pg_lock:
        pool = _pg_pools.get(chave)
        if pool is None:
            pool = await asyncpg.create_pool(
                host=config.get("host"),
                port=int(config.get("porta", 5432)),
                user=config.get("usuario"),
                password=senha,
                database=config.get("base"),
                min_size=0,
                max_size=max(1, cfg.pool_fontes_max),
                max_inactive_connection_lifetime=cfg.pool_fontes_ttl_segundos,  # recycle idle
            )
            _pg_pools[chave] = pool
    return pool


@contextlib.asynccontextmanager
async def postgres_conn(
    config: dict, senha: str | None, abrir: Callable[[], object], usar_pool: bool
) -> AsyncIterator:
    """Lends a Postgres connection. `usar_pool=False` opens and closes it (default). `True` takes one
    from the asyncpg.Pool (recycles idle via max_inactive_connection_lifetime) and returns it at the end."""
    if not usar_pool:
        conn = await abrir()
        try:
            yield conn
        finally:
            with contextlib.suppress(Exception):
                await conn.close()
        return
    pool = await _pg_obter_pool(config, senha)
    async with (
        pool.acquire() as conn
    ):  # the pool validates/recycles; returned at the end of the block
        yield conn


async def fechar_postgres() -> None:
    """Closes the Postgres pools of the current loop (shutdown / tests)."""
    import asyncio as _aio

    loop_id = id(_aio.get_running_loop())
    chaves = [k for k in _pg_pools if k[0] == loop_id]
    for k in chaves:
        pool = _pg_pools.pop(k, None)
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
