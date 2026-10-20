"""Brute-force protection for login: records attempts and blocks per account + IP.

Blocks after N failures within a sliding window (configurable); expires on its own (based on ocorrido_em).
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from ..config import get_settings
from ..timezone import agora_utc
from .models import tentativas_login


async def registrar(conn: AsyncConnection, *, email: str, ip: str | None, sucesso: bool) -> None:
    """Record a login attempt (for the brute-force count)."""
    await conn.execute(
        tentativas_login.insert(),
        {"email_tentado": email, "ip": ip, "ocorrido_em": agora_utc(), "sucesso": sucesso},
    )


async def bloqueado(conn: AsyncConnection, *, email: str, ip: str | None) -> bool:
    """True if there are too many failures for (email, ip) within the configured window."""
    cfg = get_settings()
    desde = agora_utc() - timedelta(seconds=cfg.forca_bruta_janela_segundos)
    condicoes = [
        tentativas_login.c.email_tentado == email,
        tentativas_login.c.ocorrido_em >= desde,
        tentativas_login.c.sucesso.is_(False),
    ]
    if ip is not None:
        condicoes.append(tentativas_login.c.ip == ip)
    falhas = (
        await conn.execute(select(func.count()).select_from(tentativas_login).where(*condicoes))
    ).scalar_one()
    return falhas >= cfg.forca_bruta_max
