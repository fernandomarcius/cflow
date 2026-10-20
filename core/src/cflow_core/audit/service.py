"""Audit service. `registrar(...)` inserts exactly one record into the trail, in the SAME
transaction as the business write. Immutability (append-only) is enforced in the database by the
cflow_app role (no UPDATE/DELETE on auditoria).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from .events import Acao, AtorTipo
from .models import auditoria


async def registrar(
    conn: AsyncConnection,
    *,
    acao: Acao,
    recurso_tipo: str,
    recurso_id: UUID | None = None,
    ator_id: UUID | None = None,
    ator_tipo: AtorTipo | None = None,
    antes: dict | None = None,
    depois: dict | None = None,
    request_id: str | None = None,
    ip: str | None = None,
) -> int:
    """Write an audit record using the given connection/transaction.

    MUST receive the same `conn` as the business write so both are atomic.
    Does not commit or roll back - the caller controls the transaction (cflow_core.db.transacao).

    `ator_tipo` is inferred when absent: USUARIO if there is an ator_id, otherwise SISTEMA.
    Pass ANONIMO explicitly for requests without identity.

    Returns the id of the inserted record.
    """
    if ator_tipo is None:
        ator_tipo = AtorTipo.USUARIO if ator_id is not None else AtorTipo.SISTEMA

    if ator_tipo is AtorTipo.USUARIO and ator_id is None:
        raise ValueError("ator_tipo 'usuario' requires ator_id.")
    if ator_tipo in (AtorTipo.SISTEMA, AtorTipo.ANONIMO) and ator_id is not None:
        raise ValueError(f"ator_tipo '{ator_tipo}' does not accept ator_id.")

    resultado = await conn.execute(
        auditoria.insert().returning(auditoria.c.id),
        {
            "ator_id": ator_id,
            "ator_tipo": str(ator_tipo),
            "acao": str(acao),
            "recurso_tipo": recurso_tipo,
            "recurso_id": recurso_id,
            "antes": antes,
            "depois": depois,
            "request_id": request_id,
            "ip": ip,
        },
    )
    return resultado.scalar_one()


# --- Security events. They reuse registrar(...). ---


async def registrar_login(
    conn: AsyncConnection,
    *,
    ator_id: UUID,
    request_id: str | None = None,
    ip: str | None = None,
) -> int:
    """Successful login - known identity (ator_tipo 'usuario')."""
    return await registrar(
        conn,
        acao=Acao.LOGIN,
        recurso_tipo="sessao",
        ator_id=ator_id,
        ator_tipo=AtorTipo.USUARIO,
        request_id=request_id,
        ip=ip,
    )


async def registrar_login_falha(
    conn: AsyncConnection,
    *,
    email_tentado: str,
    request_id: str | None = None,
    ip: str | None = None,
) -> int:
    """Failed login - identity not established (ator_tipo 'anonimo').

    Records the attempted e-mail; NEVER records the password or any secret.
    """
    return await registrar(
        conn,
        acao=Acao.LOGIN_FALHA,
        recurso_tipo="sessao",
        ator_tipo=AtorTipo.ANONIMO,
        depois={"email_tentado": email_tentado},
        request_id=request_id,
        ip=ip,
    )


async def registrar_logout(
    conn: AsyncConnection,
    *,
    ator_id: UUID,
    request_id: str | None = None,
    ip: str | None = None,
) -> int:
    """Logout of an identified user."""
    return await registrar(
        conn,
        acao=Acao.LOGOUT,
        recurso_tipo="sessao",
        ator_id=ator_id,
        ator_tipo=AtorTipo.USUARIO,
        request_id=request_id,
        ip=ip,
    )


async def registrar_recuperacao_iniciada(
    conn: AsyncConnection,
    *,
    email_tentado: str,
    request_id: str | None = None,
    ip: str | None = None,
) -> int:
    """Start of password recovery - identity not established (anonimo)."""
    return await registrar(
        conn,
        acao=Acao.RECUPERACAO_INICIADA,
        recurso_tipo="recuperacao_senha",
        ator_tipo=AtorTipo.ANONIMO,
        depois={"email_tentado": email_tentado},
        request_id=request_id,
        ip=ip,
    )


async def registrar_senha_trocada(
    conn: AsyncConnection,
    *,
    usuario_id: UUID,
    request_id: str | None = None,
    ip: str | None = None,
) -> int:
    """Password change completed via recovery. The user owns the password."""
    return await registrar(
        conn,
        acao=Acao.SENHA_TROCADA,
        recurso_tipo="usuario",
        recurso_id=usuario_id,
        ator_id=usuario_id,
        ator_tipo=AtorTipo.USUARIO,
        request_id=request_id,
        ip=ip,
    )


async def registrar_permissao(
    conn: AsyncConnection,
    *,
    permissao_id: UUID,
    antes: dict | None,
    depois: dict | None,
    ator_id: UUID | None = None,
    request_id: str | None = None,
    ip: str | None = None,
) -> int:
    """Permission change, with the before/after of the permission."""
    return await registrar(
        conn,
        acao=Acao.PERMISSAO,
        recurso_tipo="permissao",
        recurso_id=permissao_id,
        ator_id=ator_id,
        antes=antes,
        depois=depois,
        request_id=request_id,
        ip=ip,
    )
