"""Purge of the technical authentication tables (session, login attempt, recovery token).

Nothing here touches the audit trail - it is append-only and permanent; who logged in and when
stays recorded in `auditoria`. Only the OPERATIONAL record goes away: the hash of a token that no
longer opens anything, a login attempt from two months ago that no longer counts toward
brute-force blocking. Without this the tables grow forever: on a long-running install almost all
sessions are expired yet still sit in the most-read table of the product (one read per
authenticated request).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Each purge is (table, predicate of what is NO LONGER VALID). The predicate is deliberately shared
# between the DELETE and the dry-run count: a dry-run that counts by one criterion and deletes by
# another is not a dry-run, it is a guess.
_SESSOES = (
    "expira_em < now() - make_interval(days => :d) "
    "OR (revogada_em IS NOT NULL AND revogada_em < now() - make_interval(days => :d))"
)
_TENTATIVAS = "ocorrido_em < now() - make_interval(days => :d)"
_TOKENS = "expira_em < now() - make_interval(days => :d)"

EXPURGOS = {
    "sessoes": ("sessoes", _SESSOES),
    "tentativas_login": ("tentativas_login", _TENTATIVAS),
    "tokens_recuperacao": ("tokens_recuperacao", _TOKENS),
}


async def contar_expurgo(conn: AsyncConnection, alvo: str, *, manter_dias: int) -> int:
    """How many rows the `alvo` purge would remove today. Used by the dry run (`--simular`)."""
    tabela, predicado = EXPURGOS[alvo]
    r = await conn.execute(
        text(f"SELECT count(*) FROM {tabela} WHERE {predicado}"), {"d": manter_dias}
    )
    return int(r.scalar_one())


async def expurgar_sessoes(conn: AsyncConnection, *, manter_dias: int = 7) -> int:
    """Remove sessions that are no longer valid: expired or revoked more than `manter_dias` ago.

    The window avoids deleting right away what an incident investigation may still want to look at
    ("when was this session revoked?"). Returns how many rows were removed.
    """
    r = await conn.execute(text(f"DELETE FROM sessoes WHERE {_SESSOES}"), {"d": manter_dias})
    return r.rowcount or 0


async def expurgar_tentativas_login(conn: AsyncConnection, *, manter_dias: int = 30) -> int:
    """Remove login attempts older than `manter_dias`.

    The brute-force blocking window is a matter of minutes (`auth/forca_bruta.py`), so nothing
    beyond a few days has operational use. Returns how many rows were removed.
    """
    r = await conn.execute(
        text(f"DELETE FROM tentativas_login WHERE {_TENTATIVAS}"), {"d": manter_dias}
    )
    return r.rowcount or 0


async def expurgar_tokens_recuperacao(conn: AsyncConnection, *, manter_dias: int = 7) -> int:
    """Remove recovery tokens expired for more than `manter_dias`. Returns the row count."""
    r = await conn.execute(
        text(f"DELETE FROM tokens_recuperacao WHERE {_TOKENS}"), {"d": manter_dias}
    )
    return r.rowcount or 0
