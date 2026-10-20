"""Per-entity `sistema` flag: a flagged item is accessible only to admins in the Studio.

The flag governs DIRECT human access (list/open/edit/publish/run), NOT internal chain resolution
(page -> view -> cube, pipeline-type sources, schedules) nor the Portal (published pages remain
consumable by the operator). Denying a non-admin = generic 403 (does not reveal existence).
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import ErroAplicacao

# recurso_tipo (the same used in `permissoes`) -> header table holding the `sistema` column.
TABELA_POR_TIPO = {
    "pipeline": "pipelines",
    "cubo": "cubos",
    "view": "views",
    "pagina": "paginas",
    "biblioteca_funcao": "biblioteca_funcoes",
}


async def eh_admin(conn: AsyncConnection, usuario_id: uuid.UUID) -> bool:
    perfil = (
        await conn.execute(
            text("SELECT perfil FROM usuarios WHERE id = :id AND ativo = true"), {"id": usuario_id}
        )
    ).scalar_one_or_none()
    return perfil == "admin"


async def ve_sistema(conn: AsyncConnection, usuario_id: uuid.UUID, recurso_tipo: str) -> bool:
    """Who sees `sistema` items of this type: the admin, and the service account for the types in its
    implicit reach (`permissoes.ALCANCE_DO_SERVICO` - currently pipeline)."""
    from .permissoes import ALCANCE_DO_SERVICO

    perfil = (
        await conn.execute(
            text("SELECT perfil::text FROM usuarios WHERE id = :id AND ativo = true"),
            {"id": usuario_id},
        )
    ).scalar_one_or_none()
    return perfil == "admin" or (perfil == "servico" and recurso_tipo in ALCANCE_DO_SERVICO)


async def eh_sistema(conn: AsyncConnection, recurso_tipo: str, recurso_id: uuid.UUID) -> bool:
    """True if the resource is flagged as system. Unknown id -> False (the normal flow's 404/403
    decides; nothing is revealed here)."""
    tabela = TABELA_POR_TIPO.get(recurso_tipo)
    if tabela is None:
        return False
    val = (
        await conn.execute(text(f"SELECT sistema FROM {tabela} WHERE id = :id"), {"id": recurso_id})
    ).scalar_one_or_none()
    return bool(val)


async def exigir_acesso(
    conn: AsyncConnection, ator_id: uuid.UUID, recurso_tipo: str, recurso_id: uuid.UUID
) -> None:
    """Block reading a system item by whoever cannot see it (`ve_sistema`). Generic 403 (does not
    reveal existence)."""
    if recurso_tipo not in TABELA_POR_TIPO:
        return
    if not await eh_sistema(conn, recurso_tipo, recurso_id):
        return
    if not await ve_sistema(conn, ator_id, recurso_tipo):
        raise ErroAplicacao(codigo="acesso_negado", mensagem="Access denied.", status=403)
