"""API dependencies. Edge authentication: Bearer -> valid session -> user."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Request

from cflow_core.auth import sessoes, tokens_api
from cflow_core.db import transacao
from cflow_core.errors import ErroAplicacao


def extrair_token(request: Request) -> str | None:
    """Extract the token from the `Authorization: Bearer <token>` header."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def _nao_autenticado() -> ErroAplicacao:
    # Generic message: does not distinguish absence/expiry/revocation.
    return ErroAplicacao(
        codigo="nao_autenticado",
        mensagem="Authentication required or session expired.",
        status=401,
    )


async def usuario_atual(request: Request) -> uuid.UUID:
    """Dependency: validate the session and return the user id. Generic 401 if invalid."""
    token = extrair_token(request)
    if not token:
        raise _nao_autenticado()
    async with transacao() as conn:
        # A service-account API token has its own prefix; anything else is a session. A rejected
        # API token still falls through to the session lookup (which will not find it either) so
        # the response is the SAME generic 401, without revealing which kind it looked like.
        usuario_id = (
            await tokens_api.validar(conn, token) if tokens_api.eh_token_api(token) else None
        )
        if usuario_id is None:
            usuario_id = await sessoes.validar(conn, token)
    if usuario_id is None:
        raise _nao_autenticado()
    return usuario_id


async def _perfil_de(usuario_id: uuid.UUID) -> str | None:
    from sqlalchemy import text

    from cflow_core.db import get_engine

    async with get_engine().connect() as conn:
        return (
            await conn.execute(
                text("SELECT perfil FROM usuarios WHERE id = :id AND ativo = true"),
                {"id": usuario_id},
            )
        ).scalar_one_or_none()


@dataclass(frozen=True)
class Ator:
    """Who is acting. The `perfil` travels along because some routes scope their reach by the
    TARGET's profile (a specialist manages operators, and only them); re-querying inside the route
    would be a second read of the same data, outside the transaction that applies the effect.
    """

    id: uuid.UUID
    perfil: str


async def _autorizar(request: Request, perfis: tuple[str, ...]) -> Ator:
    usuario_id = await usuario_atual(request)
    perfil = await _perfil_de(usuario_id)
    if perfil not in perfis:
        raise ErroAplicacao(
            codigo="nao_autorizado",
            mensagem="You do not have permission to perform this action.",
            status=403,
        )
    return Ator(id=usuario_id, perfil=perfil)


def exigir_perfil(*perfis: str):
    """Dependency factory: requires a valid session AND that the user has one of the given
    profiles. Generic 403 otherwise (does not leak resource structure).
    """

    async def _dep(request: Request) -> uuid.UUID:
        return (await _autorizar(request, perfis)).id

    return _dep


def exigir_ator(*perfis: str):
    """Like `exigir_perfil`, but returns the `Ator` (id + profile) instead of the id. Use it when
    the route must decide its SCOPE from the caller's profile, not just allow or block.
    """

    async def _dep(request: Request) -> Ator:
        return await _autorizar(request, perfis)

    return _dep
