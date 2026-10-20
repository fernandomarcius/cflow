"""User management routes. Every write is audited; profile check at the edge.

Two reach bands, not two routes: **admin** manages everyone; **specialist** creates and manages
only OPERATORS (`GERIDOS_PELO_ESPECIALISTA`), and never changes a profile or touches API tokens,
which stay `SoAdmin`. The edge lets both in; narrowing by the TARGET's profile is done by the
service, in the same transaction that applies the effect (checking here would need an extra read,
outside it).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from cflow_core.auth import tokens_api
from cflow_core.auth.notificacao import entregar_token
from cflow_core.authz.perfis import GERIDOS_PELO_ESPECIALISTA, Perfil
from cflow_core.db import transacao
from cflow_core.request_context import request_id_atual
from cflow_core.timezone import para_exibicao
from cflow_core.usuarios import service

from ..deps import Ator, exigir_ator, exigir_perfil

router = APIRouter()

SoAdmin = Depends(exigir_perfil(Perfil.ADMIN))
# User management: admin (everyone) or specialist (operators only). Narrowing by target is the
# service's job.
GereUsuarios = Depends(exigir_ator(Perfil.ADMIN, Perfil.ESPECIALISTA))


class CriarUsuarioIn(BaseModel):
    email: str
    nome: str
    perfil: str


class PerfilIn(BaseModel):
    perfil: str


class AtivoIn(BaseModel):
    ativo: bool


class EmailIn(BaseModel):
    email: str


class TokenIn(BaseModel):
    nome: str
    dias: int = tokens_api.DIAS_PADRAO


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _momento(v) -> str | None:
    return para_exibicao(v).isoformat() if v else None


@router.post("/usuarios", status_code=201)
async def criar_usuario(dados: CriarUsuarioIn, request: Request, ator: Ator = GereUsuarios) -> dict:
    async with transacao() as conn:
        criado = await service.criar(
            conn,
            email=dados.email,
            nome=dados.nome,
            perfil=dados.perfil,
            ator_id=ator.id,
            ator_perfil=ator.perfil,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    # A SERVICE account has no password: there is no link to deliver; its access is the API token
    # the admin issues afterwards. `email_entregue` stays null: neither sent nor failed.
    if criado.token_senha is None:
        return {"id": str(criado.id), "email_entregue": None, "conta_servico": True}
    # Delivery of the first-password token through the channel (outside the transaction; never in
    # the response/log). A channel failure does NOT undo the creation: the user is already
    # committed, and a 500 here would leave the account standing with the clear token lost and a
    # retry hitting "e-mail already in use". The response says whether the e-mail went out;
    # `POST /usuarios/{id}/reenviar-acesso` generates a new token.
    entregue = entregar_token(dados.email.strip().lower(), criado.token_senha)
    return {"id": str(criado.id), "email_entregue": entregue}


@router.post("/usuarios/{usuario_id}/reenviar-acesso")
async def reenviar_acesso(
    usuario_id: uuid.UUID, request: Request, ator: Ator = GereUsuarios
) -> dict:
    """Generate and deliver a new password-setting token. Invalidates nothing: the old token, if
    any, stays valid until it expires (single use, see `auth.recuperacao`). A service account has
    no password: refused (`409 conta_servico_sem_senha`)."""
    ip, rid = _ip(request), request_id_atual()
    async with transacao() as conn:
        email, token = await service.reenviar_acesso(
            conn,
            usuario_id=usuario_id,
            ator_id=ator.id,
            ator_perfil=ator.perfil,
            ip=ip,
            request_id=rid,
        )
    entregue = entregar_token(email, token)
    return {"email": email, "email_entregue": entregue}


@router.patch("/usuarios/{usuario_id}/email")
async def corrigir_email(
    usuario_id: uuid.UUID, dados: EmailIn, request: Request, ator: Ator = GereUsuarios
) -> dict:
    """Correct the e-mail (delivery address AND login identifier) and resend the access link to
    the new address; correcting without resending would leave the user unable to set a password.

    A suspended account gets the e-mail corrected but NOT resent (the link would be useless):
    resending is what requires active access, and making the correction depend on it would undo
    the correction by rollback. A service account is not resent either, since it has no password.
    """
    ip, rid = _ip(request), request_id_atual()
    async with transacao() as conn:
        anterior, ativo = await service.corrigir_email(
            conn,
            usuario_id=usuario_id,
            novo_email=dados.email,
            ator_id=ator.id,
            ator_perfil=ator.perfil,
            ip=ip,
            request_id=rid,
        )
        email, token = (
            await service.reenviar_acesso(
                conn,
                usuario_id=usuario_id,
                ator_id=ator.id,
                ator_perfil=ator.perfil,
                ip=ip,
                request_id=rid,
                servico_sem_link=True,
            )
            if ativo
            else (dados.email.strip().lower(), None)
        )
    entregue = entregar_token(email, token) if token else False
    return {
        "email": email,
        "email_anterior": anterior,
        "email_entregue": entregue,
        "reenviado": token is not None,
    }


@router.patch("/usuarios/{usuario_id}/perfil", status_code=204)
async def alterar_perfil(
    usuario_id: uuid.UUID, dados: PerfilIn, request: Request, ator: uuid.UUID = SoAdmin
) -> Response:
    async with transacao() as conn:
        await service.atribuir_perfil(
            conn,
            usuario_id=usuario_id,
            novo_perfil=dados.perfil,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return Response(status_code=204)


@router.patch("/usuarios/{usuario_id}/ativo", status_code=204)
async def alterar_ativo(
    usuario_id: uuid.UUID, dados: AtivoIn, request: Request, ator: Ator = GereUsuarios
) -> Response:
    async with transacao() as conn:
        await service.definir_ativo(
            conn,
            usuario_id=usuario_id,
            ativo=dados.ativo,
            ator_id=ator.id,
            ator_perfil=ator.perfil,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return Response(status_code=204)


@router.get("/usuarios")
async def listar_usuarios(ator: Ator = GereUsuarios) -> list[dict]:
    # The specialist sees only who they manage: a row they cannot touch would become a button that
    # answers 403. The admin still sees everyone.
    perfis = None if ator.perfil == Perfil.ADMIN else GERIDOS_PELO_ESPECIALISTA
    async with transacao() as conn:
        linhas = await service.listar(conn, perfis=perfis)
    return [
        {
            "id": str(u["id"]),
            "email": u["email"],
            "nome": u["nome"],
            "perfil": u["perfil"],
            "ativo": u["ativo"],
            "ultimo_acesso": _momento(u["ultimo_acesso"]),
        }
        for u in linhas
    ]


# --- Service-account API tokens ----------------------------------------------------------------


@router.post("/usuarios/{usuario_id}/tokens", status_code=201)
async def emitir_token(
    usuario_id: uuid.UUID, dados: TokenIn, request: Request, ator: uuid.UUID = SoAdmin
) -> dict:
    """Issue an API token for the SERVICE account. The value comes back ONCE, in this response;
    only the hash is stored, so it cannot be read back later."""
    async with transacao() as conn:
        t = await tokens_api.emitir(
            conn,
            usuario_id=usuario_id,
            nome=dados.nome,
            dias=dados.dias,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {
        "id": str(t.id),
        "token": t.token,
        "prefixo": t.prefixo,
        "expira_em": _momento(t.expira_em),
    }


@router.get("/usuarios/{usuario_id}/tokens")
async def listar_tokens(usuario_id: uuid.UUID, ator: uuid.UUID = SoAdmin) -> list[dict]:
    """The account's tokens, without secrets: name, prefix, expiry, last use and revocation."""
    async with transacao() as conn:
        linhas = await tokens_api.listar(conn, usuario_id)
    return [
        {
            "id": str(t["id"]),
            "nome": t["nome"],
            "prefixo": t["prefixo"],
            "criado_em": _momento(t["criado_em"]),
            "expira_em": _momento(t["expira_em"]),
            "ultimo_uso_em": _momento(t["ultimo_uso_em"]),
            "revogado_em": _momento(t["revogado_em"]),
        }
        for t in linhas
    ]


@router.delete("/usuarios/{usuario_id}/tokens/{token_id}", status_code=204)
async def revogar_token(
    usuario_id: uuid.UUID, token_id: uuid.UUID, request: Request, ator: uuid.UUID = SoAdmin
) -> Response:
    """Revoke immediately: the next request with the token gets 401. Idempotent."""
    async with transacao() as conn:
        await tokens_api.revogar(
            conn,
            usuario_id=usuario_id,
            token_id=token_id,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return Response(status_code=204)
