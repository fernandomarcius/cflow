"""Per-resource permissions: effective-level computation (direct ∪ groups) and grant/revoke.

There is no explicit "deny": no permission = no access; the highest level wins.
"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from ..timezone import agora_utc
from .models import permissoes, usuarios_grupos
from .perfis import Nivel, Perfil, cobre, rank

# SERVICE account: reaches EVERY pipeline - existing and future ones, flagged `sistema` or not -
# up to `executar`. Per-pipeline grants would have to be issued one by one: every new pipeline an
# integrator triggers would show up as "pipeline does not exist" until someone remembered to grant
# access, and the symptom is a business 404, not a 403. The ceiling stays `executar`: the account
# triggers and follows runs, it does not edit or administer - authoring is still blocked at the
# edge by profile.
ALCANCE_DO_SERVICO: dict[str, str] = {"pipeline": Nivel.EXECUTAR}


def servico_alcanca(perfil: str | None, recurso_tipo: str, nivel_exigido: str) -> bool:
    """True if the account is a service account and the resource/level is within its implicit reach."""
    if perfil != Perfil.SERVICO:
        return False
    teto = ALCANCE_DO_SERVICO.get(recurso_tipo)
    return teto is not None and cobre(teto, nivel_exigido)


async def _grupos_do_usuario(conn: AsyncConnection, usuario_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (
        (
            await conn.execute(
                select(usuarios_grupos.c.grupo_id).where(usuarios_grupos.c.usuario_id == usuario_id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def nivel_efetivo(
    conn: AsyncConnection, usuario_id: uuid.UUID, recurso_tipo: str, recurso_id: uuid.UUID
) -> str | None:
    """Highest level the user has on the resource (direct ∪ group permissions). None if none."""
    grupos = await _grupos_do_usuario(conn, usuario_id)
    sujeito = or_(
        and_(permissoes.c.sujeito_tipo == "usuario", permissoes.c.sujeito_id == usuario_id),
        and_(permissoes.c.sujeito_tipo == "grupo", permissoes.c.sujeito_id.in_(grupos))
        if grupos
        else and_(False),
    )
    niveis = (
        (
            await conn.execute(
                select(permissoes.c.nivel).where(
                    sujeito,
                    permissoes.c.recurso_tipo == recurso_tipo,
                    permissoes.c.recurso_id == recurso_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not niveis:
        return None
    return max(niveis, key=rank)


async def tem_permissao(
    conn: AsyncConnection,
    usuario_id: uuid.UUID,
    recurso_tipo: str,
    recurso_id: uuid.UUID,
    nivel_exigido: str,
) -> bool:
    """True if the effective level covers the required one (service account: see `ALCANCE_DO_SERVICO`)."""
    if servico_alcanca(await _perfil(conn, usuario_id), recurso_tipo, nivel_exigido):
        return True
    efetivo = await nivel_efetivo(conn, usuario_id, recurso_tipo, recurso_id)
    return efetivo is not None and cobre(efetivo, nivel_exigido)


async def _perfil(conn: AsyncConnection, usuario_id: uuid.UUID) -> str | None:
    from sqlalchemy import text

    return (
        await conn.execute(text("SELECT perfil FROM usuarios WHERE id = :id"), {"id": usuario_id})
    ).scalar_one_or_none()


async def exigir(
    conn: AsyncConnection,
    usuario_id: uuid.UUID,
    recurso_tipo: str,
    recurso_id: uuid.UUID,
    nivel: str,
) -> None:
    """Per-resource gate: admin always passes (administers everything); a service account passes within
    its implicit reach (`ALCANCE_DO_SERVICO`, including `sistema` items); otherwise an effective level
    on the resource is required. Denial = generic 403 (does not reveal existence)."""
    from ..errors import ErroAplicacao

    perfil = await _perfil(conn, usuario_id)
    if perfil == "admin":
        return
    if servico_alcanca(perfil, recurso_tipo, nivel):
        return
    # Item flagged as system: admin only (who already returned above). Overrides any fine-grained grant.
    from . import sistema

    if await sistema.eh_sistema(conn, recurso_tipo, recurso_id):
        raise ErroAplicacao(codigo="acesso_negado", mensagem="Access denied.", status=403)
    if await tem_permissao(conn, usuario_id, recurso_tipo, recurso_id, nivel):
        return
    raise ErroAplicacao(codigo="acesso_negado", mensagem="Access denied.", status=403)


async def exigir_concessao(
    conn: AsyncConnection,
    usuario_id: uuid.UUID,
    recurso_tipo: str,
    recurso_id: uuid.UUID,
    nivel: str,
) -> None:
    """Require a real contextual grant, admins included, without changing the global ``exigir`` gate."""
    from ..errors import ErroAplicacao

    if await tem_permissao(conn, usuario_id, recurso_tipo, recurso_id, nivel):
        return
    raise ErroAplicacao(codigo="acesso_negado", mensagem="Access denied.", status=403)


async def conceder(
    conn: AsyncConnection,
    *,
    sujeito_tipo: str,
    sujeito_id: uuid.UUID,
    recurso_tipo: str,
    recurso_id: uuid.UUID,
    nivel: str,
    concedido_por: uuid.UUID | None,
) -> bool:
    """Grant the permission. Idempotent: an identical existing one is not duplicated. Returns True if created."""
    existe = (
        await conn.execute(
            select(permissoes.c.id).where(
                permissoes.c.sujeito_tipo == sujeito_tipo,
                permissoes.c.sujeito_id == sujeito_id,
                permissoes.c.recurso_tipo == recurso_tipo,
                permissoes.c.recurso_id == recurso_id,
                permissoes.c.nivel == nivel,
            )
        )
    ).first()
    if existe is not None:
        return False
    await conn.execute(
        permissoes.insert(),
        {
            "id": uuid.uuid4(),
            "sujeito_tipo": sujeito_tipo,
            "sujeito_id": sujeito_id,
            "recurso_tipo": recurso_tipo,
            "recurso_id": recurso_id,
            "nivel": nivel,
            "concedido_por": concedido_por,
            "criado_em": agora_utc(),
        },
    )
    return True


async def revogar(
    conn: AsyncConnection,
    *,
    sujeito_tipo: str,
    sujeito_id: uuid.UUID,
    recurso_tipo: str,
    recurso_id: uuid.UUID,
    nivel: str,
) -> None:
    """Revoke the matching permission. Idempotent (does not fail if it does not exist)."""
    await conn.execute(
        delete(permissoes).where(
            permissoes.c.sujeito_tipo == sujeito_tipo,
            permissoes.c.sujeito_id == sujeito_id,
            permissoes.c.recurso_tipo == recurso_tipo,
            permissoes.c.recurso_id == recurso_id,
            permissoes.c.nivel == nivel,
        )
    )
