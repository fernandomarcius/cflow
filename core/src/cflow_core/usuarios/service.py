"""User management. Soft delete (never erases), password via the recovery-token flow, last-admin
protection, and auditing of every write in the same transaction.

Who manages whom: `ator_perfil` is REQUIRED on every write and goes through `pode_gerir_usuario`.
The admin manages everyone; the specialist manages only operators (create, resend access, fix
e-mail, suspend/restore) and NEVER changes profiles - `atribuir_perfil` stays admin-only at the
edge, which is what stops a specialist from creating an operator and promoting it to admin a
minute later.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..audit import Acao, registrar
from ..auth.recuperacao import iniciar_definicao_senha
from ..authz.models import usuarios
from ..authz.perfis import Perfil, pode_gerir_usuario
from ..errors import ErroAplicacao
from ..timezone import agora_utc


@dataclass
class UsuarioCriado:
    id: uuid.UUID
    # first-password token (delivered through the channel; never logged). None for a SERVICE
    # account, which has no password: it signs in with an API token.
    token_senha: str | None


async def _admins_ativos(conn: AsyncConnection, excluir: uuid.UUID | None = None) -> int:
    cond = [usuarios.c.perfil == Perfil.ADMIN, usuarios.c.ativo.is_(True)]
    if excluir is not None:
        cond.append(usuarios.c.id != excluir)
    return (
        await conn.execute(select(func.count()).select_from(usuarios).where(*cond))
    ).scalar_one()


async def _buscar(conn: AsyncConnection, usuario_id: uuid.UUID):
    return (
        await conn.execute(
            select(usuarios.c.id, usuarios.c.perfil, usuarios.c.ativo, usuarios.c.email).where(
                usuarios.c.id == usuario_id
            )
        )
    ).first()


def _exigir_gestao(ator_perfil: str, alvo) -> None:
    """Refuse when the actor does not manage that target. A non-admin actor gets a **generic 403
    even when the target does not exist**: a 404 there would tell a specialist that the id exists
    and belongs to a profile they cannot reach (denying must not reveal existence).
    """
    if alvo is not None and pode_gerir_usuario(ator_perfil, alvo.perfil):
        return
    if alvo is None and ator_perfil == Perfil.ADMIN:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="User not found.", status=404)
    raise ErroAplicacao(
        codigo="nao_autorizado",
        mensagem="You do not have permission for this action.",
        status=403,
    )


async def criar(
    conn: AsyncConnection,
    *,
    email: str,
    nome: str,
    perfil: str,
    ator_id: uuid.UUID,
    ator_perfil: str,
    ip: str | None = None,
    request_id: str | None = None,
) -> UsuarioCriado:
    if perfil not in set(Perfil):
        raise ErroAplicacao(codigo="perfil_invalido", mensagem="Invalid profile.", status=400)
    # An invalid profile is checked FIRST: a mistyped "admin" is a 400, not a 403 that suggests the
    # value exists and only permission is missing.
    if not pode_gerir_usuario(ator_perfil, perfil):
        raise ErroAplicacao(
            codigo="nao_autorizado",
            mensagem="You do not have permission to create a user with this profile.",
            status=403,
        )
    # Normalize here and in `corrigir_email` with the same rule: the e-mail is the login identifier
    # and lookup is by exact equality, so storing it as typed would create an account unreachable
    # because of letter case (the front end already sends lowercase; the API cannot rely on that).
    email = email.strip().lower()
    existe = (await conn.execute(select(usuarios.c.id).where(usuarios.c.email == email))).first()
    if existe is not None:
        raise ErroAplicacao(codigo="email_em_uso", mensagem="E-mail is already in use.", status=409)

    novo_id = uuid.uuid4()
    await conn.execute(
        usuarios.insert(),
        {
            "id": novo_id,
            "email": email,
            "nome": nome,
            "perfil": perfil,
            "ativo": True,
            "senha_hash": None,
            "ultimo_acesso": None,
            "criado_em": agora_utc(),
        },
    )
    await registrar(
        conn,
        acao=Acao.CRIAR,
        recurso_tipo="usuario",
        recurso_id=novo_id,
        ator_id=ator_id,
        antes=None,
        depois={"email": email, "nome": nome, "perfil": perfil},
        request_id=request_id,
        ip=ip,
    )
    token = (
        None
        if perfil == Perfil.SERVICO
        else await iniciar_definicao_senha(conn, novo_id, primeira=True)
    )
    return UsuarioCriado(id=novo_id, token_senha=token)


async def corrigir_email(
    conn: AsyncConnection,
    *,
    usuario_id: uuid.UUID,
    novo_email: str,
    ator_id: uuid.UUID,
    ator_perfil: str,
    ip: str | None = None,
    request_id: str | None = None,
) -> tuple[str, bool]:
    """Fix a user's e-mail. Returns (previous e-mail, access active).

    It exists because the e-mail is both the delivery address of the first-access link AND the
    login identifier: a typo (`@exmaple.com` instead of `@example.com`) left the user unreachable,
    and since the platform never deletes users, the only way out was to suspend and recreate.
    """
    novo = novo_email.strip().lower()
    if not novo:
        raise ErroAplicacao(codigo="email_invalido", mensagem="Provide the e-mail.", status=400)
    alvo = await _buscar(conn, usuario_id)
    _exigir_gestao(ator_perfil, alvo)
    if novo == alvo.email:
        return alvo.email, alvo.ativo
    existe = (
        await conn.execute(
            select(usuarios.c.id).where(usuarios.c.email == novo, usuarios.c.id != usuario_id)
        )
    ).first()
    if existe is not None:
        raise ErroAplicacao(codigo="email_em_uso", mensagem="E-mail is already in use.", status=409)
    await conn.execute(usuarios.update().where(usuarios.c.id == usuario_id).values(email=novo))
    await registrar(
        conn,
        acao=Acao.EDITAR,
        recurso_tipo="usuario",
        recurso_id=usuario_id,
        ator_id=ator_id,
        antes={"email": alvo.email},
        depois={"email": novo},
        request_id=request_id,
        ip=ip,
    )
    return alvo.email, alvo.ativo


async def reenviar_acesso(
    conn: AsyncConnection,
    *,
    usuario_id: uuid.UUID,
    ator_id: uuid.UUID,
    ator_perfil: str,
    ip: str | None = None,
    request_id: str | None = None,
    servico_sem_link: bool = False,
) -> tuple[str, str | None]:
    """Generate a new password-setup token for an existing user. Returns (email, token).

    A SERVICE account has no password: an explicit request is refused; a caller that only fixes the
    e-mail (`servico_sem_link=True`) gets `token=None` - fixing must not be undone by a rollback.

    Closes the lost-token gap: the clear-text token only exists in the `criar` response, so any
    delivery failure left the user unable to set a password, with no way to recover from the UI.
    The token goes through the channel; never in the API response, never in the log, never in the trail.
    """
    alvo = await _buscar(conn, usuario_id)
    _exigir_gestao(ator_perfil, alvo)
    if not alvo.ativo:
        raise ErroAplicacao(
            codigo="usuario_suspenso",
            mensagem="Access is suspended: restore access before resending the link.",
            status=409,
        )
    if alvo.perfil == Perfil.SERVICO:
        if servico_sem_link:
            return alvo.email, None
        raise ErroAplicacao(
            codigo="conta_servico_sem_senha",
            mensagem="A service account has no password or access link: it signs in with an API "
            "token, issued on the users screen.",
            status=409,
        )
    token = await iniciar_definicao_senha(conn, usuario_id, primeira=True)
    await registrar(
        conn,
        acao=Acao.RECUPERACAO_INICIADA,
        recurso_tipo="usuario",
        recurso_id=usuario_id,
        ator_id=ator_id,
        antes=None,
        request_id=request_id,
        ip=ip,
        depois={"motivo": "reenvio_link_de_acesso", "email": alvo.email},
    )
    return alvo.email, token


async def atribuir_perfil(
    conn: AsyncConnection,
    *,
    usuario_id: uuid.UUID,
    novo_perfil: str,
    ator_id: uuid.UUID,
    ip: str | None = None,
    request_id: str | None = None,
) -> None:
    if novo_perfil not in set(Perfil):
        raise ErroAplicacao(codigo="perfil_invalido", mensagem="Invalid profile.", status=400)
    # No `ator_perfil`: changing profile is admin-only, enforced at the edge (`SoAdmin`). Calling
    # `pode_gerir_usuario` here would suggest another profile may one day get in - and this is
    # exactly the door that must stay shut.
    alvo = await _buscar(conn, usuario_id)
    if alvo is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="User not found.", status=404)
    # last-admin protection: demoting the last active admin
    if alvo.perfil == Perfil.ADMIN and novo_perfil != Perfil.ADMIN and alvo.ativo:
        if await _admins_ativos(conn, excluir=usuario_id) < 1:
            raise ErroAplicacao(
                codigo="ultimo_admin",
                mensagem="Cannot demote the last active admin.",
                status=409,
            )
    await conn.execute(
        usuarios.update().where(usuarios.c.id == usuario_id).values(perfil=novo_perfil)
    )
    depois: dict = {"perfil": novo_perfil}
    if novo_perfil == Perfil.SERVICO and alvo.perfil != Perfil.SERVICO:
        # A service account signs in only by token: a session opened with a password would no longer
        # be valid by the rule yet would keep working until it expires.
        depois["sessoes_revogadas"] = (
            await conn.execute(
                text(
                    "UPDATE sessoes SET revogada_em = now() "
                    "WHERE usuario_id = :u AND revogada_em IS NULL"
                ),
                {"u": usuario_id},
            )
        ).rowcount
    await registrar(
        conn,
        acao=Acao.EDITAR,
        recurso_tipo="perfil",
        recurso_id=usuario_id,
        ator_id=ator_id,
        antes={"perfil": alvo.perfil},
        depois=depois,
        request_id=request_id,
        ip=ip,
    )


async def definir_ativo(
    conn: AsyncConnection,
    *,
    usuario_id: uuid.UUID,
    ativo: bool,
    ator_id: uuid.UUID,
    ator_perfil: str,
    ip: str | None = None,
    request_id: str | None = None,
) -> None:
    alvo = await _buscar(conn, usuario_id)
    _exigir_gestao(ator_perfil, alvo)
    if not ativo and alvo.perfil == Perfil.ADMIN and alvo.ativo:
        if await _admins_ativos(conn, excluir=usuario_id) < 1:
            raise ErroAplicacao(
                codigo="ultimo_admin",
                mensagem="Cannot deactivate the last active admin.",
                status=409,
            )
    await conn.execute(usuarios.update().where(usuarios.c.id == usuario_id).values(ativo=ativo))
    await registrar(
        conn,
        acao=Acao.EDITAR,
        recurso_tipo="usuario",
        recurso_id=usuario_id,
        ator_id=ator_id,
        antes={"ativo": alvo.ativo},
        depois={"ativo": ativo},
        request_id=request_id,
        ip=ip,
    )


async def listar(conn: AsyncConnection, *, perfis: Collection[str] | None = None) -> list[dict]:
    """List users. `perfis` filters by profile: the specialist receives only what they manage, so
    no row on screen offers an action that would answer 403.
    """
    consulta = select(
        usuarios.c.id,
        usuarios.c.email,
        usuarios.c.nome,
        usuarios.c.perfil,
        usuarios.c.ativo,
        usuarios.c.ultimo_acesso,
    )
    if perfis is not None:
        consulta = consulta.where(usuarios.c.perfil.in_(list(perfis)))
    rows = (await conn.execute(consulta.order_by(usuarios.c.criado_em))).mappings().all()
    return [dict(r) for r in rows]
