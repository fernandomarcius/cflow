"""Server-side sessions. A random opaque token goes to the client; only its hash (SHA-256 -
the token is already high-entropy) is stored. Expires by inactivity and by absolute time; revocable.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from ..config import get_settings
from ..timezone import agora_utc
from .models import sessoes


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class SessaoCriada:
    token: str  # clear text - delivered to the client only once
    expira_em: datetime


async def criar(conn: AsyncConnection, usuario_id: uuid.UUID) -> SessaoCriada:
    """Create a session and return the clear-text opaque token (only here)."""
    cfg = get_settings()
    agora = agora_utc()
    token = secrets.token_urlsafe(32)
    expira_em = agora + timedelta(seconds=cfg.sessao_absoluta_segundos)
    await conn.execute(
        sessoes.insert(),
        {
            "id": uuid.uuid4(),
            "usuario_id": usuario_id,
            "token_hash": _hash_token(token),
            "criada_em": agora,
            "ultima_atividade_em": agora,
            "expira_em": expira_em,
            "revogada_em": None,
        },
    )
    return SessaoCriada(token=token, expira_em=expira_em)


async def validar(conn: AsyncConnection, token: str) -> uuid.UUID | None:
    """Validate the token; if the session is active, update the last activity and return the user.

    Returns None if missing, revoked, expired (absolute) or inactive beyond the limit.
    """
    cfg = get_settings()
    agora = agora_utc()
    th = _hash_token(token)
    row = (
        await conn.execute(
            select(sessoes.c.usuario_id, sessoes.c.expira_em, sessoes.c.ultima_atividade_em).where(
                sessoes.c.token_hash == th, sessoes.c.revogada_em.is_(None)
            )
        )
    ).first()
    if row is None:
        return None
    if agora >= row.expira_em:
        return None
    if (agora - row.ultima_atividade_em) > timedelta(seconds=cfg.sessao_inatividade_segundos):
        return None
    await conn.execute(
        update(sessoes).where(sessoes.c.token_hash == th).values(ultima_atividade_em=agora)
    )
    return row.usuario_id


async def revogar(conn: AsyncConnection, token: str) -> None:
    """Revoke the session (logout). Idempotent."""
    await conn.execute(
        update(sessoes)
        .where(sessoes.c.token_hash == _hash_token(token), sessoes.c.revogada_em.is_(None))
        .values(revogada_em=agora_utc())
    )
