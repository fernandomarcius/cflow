"""MFA flow: configure (generate secret -> vault), activate (confirm TOTP) and validate at login.

The TOTP secret lives encrypted in the vault; only the reference is kept in `usuarios.mfa_credencial_ref`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import ErroAplicacao
from ..vault import service as vault
from . import mfa


async def configurar(conn: AsyncConnection, usuario_id: uuid.UUID) -> tuple[str, str]:
    """Generate a TOTP secret, store it in the vault and return (secret, otpauth URI). Does NOT enable MFA yet."""
    row = (
        await conn.execute(text("SELECT email FROM usuarios WHERE id = :id"), {"id": usuario_id})
    ).first()
    if row is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="User not found.", status=404)
    segredo = mfa.gerar_segredo()
    ref = await vault.guardar(conn, segredo)
    await conn.execute(
        text("UPDATE usuarios SET mfa_credencial_ref = :r, mfa_habilitado = false WHERE id = :id"),
        {"r": ref, "id": usuario_id},
    )
    return segredo, mfa.uri_provisionamento(row.email, segredo)


async def _segredo(conn: AsyncConnection, usuario_id: uuid.UUID) -> str | None:
    ref = (
        await conn.execute(
            text("SELECT mfa_credencial_ref FROM usuarios WHERE id = :id"), {"id": usuario_id}
        )
    ).scalar_one_or_none()
    return await vault.resolver(conn, ref) if ref else None


async def ativar(conn: AsyncConnection, usuario_id: uuid.UUID, codigo: str) -> None:
    """Confirm enrollment: validate the TOTP and set mfa_habilitado. Raises 400 if the code does not match."""
    segredo = await _segredo(conn, usuario_id)
    if not segredo or not mfa.verificar_totp(segredo, codigo):
        raise ErroAplicacao(codigo="mfa_invalido", mensagem="Invalid MFA code.", status=400)
    await conn.execute(
        text("UPDATE usuarios SET mfa_habilitado = true WHERE id = :id"), {"id": usuario_id}
    )


async def desativar(conn: AsyncConnection, usuario_id: uuid.UUID, senha_ok: bool) -> None:
    """Turn off the user's OWN second factor. Requires the caller to have verified the current password
    (`senha_ok`): otherwise a browser left open could disable the account's second factor.
    The secret reference is erased - re-enabling generates a new one (the previous one cannot be restored)."""
    if not senha_ok:
        raise ErroAplicacao(
            codigo="senha_atual_invalida", mensagem="Current password is incorrect.", status=400
        )
    await conn.execute(
        text(
            "UPDATE usuarios SET mfa_habilitado = false, mfa_credencial_ref = NULL WHERE id = :id"
        ),
        {"id": usuario_id},
    )


async def exige_mfa(conn: AsyncConnection, usuario_id: uuid.UUID) -> bool:
    return bool(
        (
            await conn.execute(
                text("SELECT mfa_habilitado FROM usuarios WHERE id = :id"), {"id": usuario_id}
            )
        ).scalar_one_or_none()
    )


async def validar_login(conn: AsyncConnection, usuario_id: uuid.UUID, codigo: str | None) -> bool:
    """At login, validate the second factor when the user has MFA. Missing or invalid code -> False."""
    segredo = await _segredo(conn, usuario_id)
    if not segredo or not codigo:
        return False
    return mfa.verificar_totp(segredo, codigo)
