"""Credential vault. The secret is encrypted (Fernet) before being persisted; only the reference
travels outside the vault. `resolver` is used ONLY in the worker, at runtime, when testing/using
the connection.
"""

from __future__ import annotations

import uuid
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..config import get_settings
from ..errors import ErroAplicacao
from ..timezone import agora_utc


@lru_cache
def _fernet():
    from cryptography.fernet import Fernet

    chave = get_settings().vault_key
    if not chave:
        raise ErroAplicacao(
            codigo="cofre_indisponivel",
            mensagem="Credential vault is not configured.",
            status=503,
        )
    return Fernet(chave.encode() if isinstance(chave, str) else chave)


async def guardar(conn: AsyncConnection, segredo: str) -> uuid.UUID:
    """Encrypts and stores the secret. Returns the reference (uuid)."""
    cifrado = _fernet().encrypt(segredo.encode())
    ref = (
        await conn.execute(
            text("INSERT INTO cofre_credenciais (segredo_cifrado) VALUES (:s) RETURNING id"),
            {"s": cifrado},
        )
    ).scalar_one()
    return ref


async def resolver(conn: AsyncConnection, ref: uuid.UUID) -> str | None:
    """Resolves the plaintext secret (worker/runtime only). None if the ref does not exist."""
    cifrado = (
        await conn.execute(
            text("SELECT segredo_cifrado FROM cofre_credenciais WHERE id = :id"), {"id": ref}
        )
    ).scalar_one_or_none()
    if cifrado is None:
        return None
    return _fernet().decrypt(bytes(cifrado)).decode()


async def copiar(conn: AsyncConnection, ref: uuid.UUID) -> uuid.UUID:
    """New reference with the SAME secret: the encrypted row is copied inside the database, without decrypting.

    Used when duplicating a connection. Whoever duplicates never sees or retypes the credential, and
    the API never holds it in plaintext: the secret is still resolved only in the worker, at runtime.
    The rows are independent: rotating or deleting one does not affect the other."""
    nova = (
        await conn.execute(
            text(
                "INSERT INTO cofre_credenciais (segredo_cifrado) "
                "SELECT segredo_cifrado FROM cofre_credenciais WHERE id = :id RETURNING id"
            ),
            {"id": ref},
        )
    ).scalar_one_or_none()
    if nova is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Credential not found.", status=404)
    return nova


async def rotacionar(conn: AsyncConnection, ref: uuid.UUID, novo_segredo: str) -> None:
    """Replaces the secret value keeping the same reference (connections need no editing)."""
    cifrado = _fernet().encrypt(novo_segredo.encode())
    res = await conn.execute(
        text(
            "UPDATE cofre_credenciais SET segredo_cifrado = :s, atualizado_em = :t WHERE id = :id"
        ),
        {"s": cifrado, "t": agora_utc(), "id": ref},
    )
    if res.rowcount == 0:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Credential not found.", status=404)
