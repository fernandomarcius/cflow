"""Password recovery via single-use token. Stores only the token hash; validates and consumes it.

The password policy is checked BEFORE the token is consumed (a weak password does not burn the token).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from ..config import get_settings
from ..errors import ErroAplicacao
from ..timezone import agora_utc
from .models import tokens_recuperacao
from .senhas import gerar_hash, validar_politica


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _gerar_token(conn: AsyncConnection, usuario_id: uuid.UUID, *, segundos: int) -> str:
    """Create a single-use token for the user, with the given window. Returns the clear-text token."""
    agora = agora_utc()
    token = secrets.token_urlsafe(32)
    await conn.execute(
        tokens_recuperacao.insert(),
        {
            "id": uuid.uuid4(),
            "usuario_id": usuario_id,
            "token_hash": _hash_token(token),
            "criado_em": agora,
            "expira_em": agora + timedelta(seconds=segundos),
            "usado_em": None,
        },
    )
    return token


async def solicitar(conn: AsyncConnection, email: str) -> str | None:
    """Generate a recovery token if the e-mail exists. Returns the clear-text token (for the channel)
    or None if the e-mail does not exist (the caller responds generically either way).
    """
    row = (
        await conn.execute(text("SELECT id FROM usuarios WHERE email = :e"), {"e": email})
    ).first()
    if row is None:
        return None
    return await _gerar_token(conn, row.id, segundos=get_settings().recuperacao_expira_segundos)


async def iniciar_definicao_senha(
    conn: AsyncConnection, usuario_id: uuid.UUID, *, primeira: bool = True
) -> str:
    """Start password setup for a user (the first password uses a longer window, 72h by default).
    Reuses the same single-use token mechanism as recovery. Returns the clear-text token.
    """
    cfg = get_settings()
    segundos = cfg.primeira_senha_expira_segundos if primeira else cfg.recuperacao_expira_segundos
    return await _gerar_token(conn, usuario_id, segundos=segundos)


async def confirmar(conn: AsyncConnection, token: str, nova_senha: str) -> uuid.UUID:
    """Set the new password using a valid token. Raises ErroAplicacao (generic 400) if the token
    is invalid/expired/used, or if the password violates the policy. Returns the usuario_id.
    """
    th = _hash_token(token)
    agora = agora_utc()
    row = (
        await conn.execute(
            select(tokens_recuperacao.c.id, tokens_recuperacao.c.usuario_id).where(
                tokens_recuperacao.c.token_hash == th,
                tokens_recuperacao.c.usado_em.is_(None),
                tokens_recuperacao.c.expira_em > agora,
            )
        )
    ).first()
    if row is None:
        raise ErroAplicacao(
            codigo="token_invalido",
            mensagem="Invalid or expired recovery token.",
            status=400,
        )

    # validate the policy BEFORE consuming the token
    validar_politica(nova_senha)

    await conn.execute(
        text("UPDATE usuarios SET senha_hash = :h WHERE id = :id"),
        {"h": gerar_hash(nova_senha), "id": row.usuario_id},
    )
    await conn.execute(
        update(tokens_recuperacao).where(tokens_recuperacao.c.id == row.id).values(usado_em=agora)
    )
    return row.usuario_id
