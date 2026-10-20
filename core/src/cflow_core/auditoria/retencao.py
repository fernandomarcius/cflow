"""Retention policy for TECHNICAL LOGS (never the audit trail, which is permanent).

The trail (`auditoria`) is NEVER referenced here - the separation is inviolable.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

CHAVE = "log_retencao_dias"


async def ler_retencao_dias(conn: AsyncConnection) -> int:
    v = (
        await conn.execute(text("SELECT valor FROM configuracoes WHERE chave = :k"), {"k": CHAVE})
    ).scalar_one_or_none()
    return int(v) if v is not None else 90


async def definir_retencao_dias(conn: AsyncConnection, dias: int) -> int:
    """Update the policy; return the previous value (so the caller can audit it)."""
    if dias < 1:
        from ..errors import ErroAplicacao

        raise ErroAplicacao(
            codigo="valor_invalido", mensagem="Retention must be at least 1 day.", status=400
        )
    anterior = await ler_retencao_dias(conn)
    await conn.execute(
        text(
            "INSERT INTO configuracoes (chave, valor) VALUES (:k, :v) "
            "ON CONFLICT (chave) DO UPDATE SET valor = :v, atualizado_em = now()"
        ),
        {"k": CHAVE, "v": str(dias)},
    )
    return anterior


async def expurgar_logs_tecnicos(conn: AsyncConnection, *, dias: int | None = None) -> int:
    """Purge technical logs beyond retention. Does NOT touch the audit trail.

    The platform's technical logs are emitted as JSON on stdout (collected by the
    infrastructure), so there is no log table to purge here - this function exists for the
    retention routine and, by contract, NEVER references the `auditoria` table. Returns the
    number of logs purged by the application (0 while logs live outside the database).
    """
    return 0
