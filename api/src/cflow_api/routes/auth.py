"""Authentication routes: login, logout, password recovery, SSO and MFA.

Refusals are generic. Events are audited in the same transaction as the write.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import text

from cflow_core.audit import (
    Acao,
    registrar,
    registrar_login,
    registrar_login_falha,
    registrar_logout,
    registrar_recuperacao_iniciada,
    registrar_senha_trocada,
)
from cflow_core.auth import forca_bruta, mfa_fluxo, recuperacao, sessoes, sso
from cflow_core.auth.notificacao import entregar_token
from cflow_core.auth.senhas import gerar_hash, validar_politica, verificar
from cflow_core.db import transacao
from cflow_core.errors import ErroAplicacao
from cflow_core.request_context import request_id_atual
from cflow_core.timezone import para_exibicao

from ..deps import extrair_token, usuario_atual

router = APIRouter()


class LoginIn(BaseModel):
    email: str
    senha: str
    codigo: str | None = None  # 2nd factor (TOTP), when the user has MFA


class RecuperacaoIn(BaseModel):
    email: str


class ConfirmarRecuperacaoIn(BaseModel):
    token: str
    nova_senha: str


class TrocarSenhaIn(BaseModel):
    senha_atual: str
    nova_senha: str


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/auth/login")
async def login(dados: LoginIn, request: Request) -> dict:
    ip = _ip(request)
    rid = request_id_atual()

    # Single transaction: records the attempt + audit + (if ok) the session. Commit, then signal.
    async with transacao() as conn:
        if await forca_bruta.bloqueado(conn, email=dados.email, ip=ip):
            estado: tuple = ("bloqueado",)
        else:
            row = (
                await conn.execute(
                    text("SELECT id, senha_hash, ativo, perfil FROM usuarios WHERE email = :e"),
                    {"e": dados.email},
                )
            ).first()
            # A SERVICE account signs in only through an API token: its password, if one is left
            # over from an earlier life, does not open a session, and the refusal is the usual
            # generic response.
            senha_ok = bool(
                row
                and row.ativo
                and row.perfil != "servico"
                and verificar(row.senha_hash, dados.senha)
            )
            mfa_ok = True
            if senha_ok and await mfa_fluxo.exige_mfa(conn, row.id):
                mfa_ok = await mfa_fluxo.validar_login(conn, row.id, dados.codigo)
            if senha_ok and mfa_ok:
                # Records `ultimo_acesso` (the users screen relies on it to show "awaiting
                # first access").
                #
                # The UPDATE also TIES the login to the hash it verified. The SELECT above does
                # not lock the row: a password change (`scripts/conta_servico.py
                # --reset-password`) committing between `verificar` and this point would let the
                # session be born AFTER the `UPDATE sessoes` that revokes it, leaving a Bearer for
                # the old password alive for 24 h while the trail says "sessions revoked". Here
                # READ COMMITTED re-evaluates the WHERE against the new hash (0 rows -> login
                # fails). In the opposite order, this row lock makes the change wait for the login
                # to commit, and its `UPDATE sessoes` then sees the created session. It runs
                # BEFORE creating the session: if refused there is no session to undo, and the
                # attempt is recorded as a failure (trail and brute-force counter).
                amarrado = await conn.execute(
                    text(
                        "UPDATE usuarios SET ultimo_acesso = now() "
                        "WHERE id = :id AND senha_hash = :hash_lido AND ativo"
                    ),
                    {"id": row.id, "hash_lido": row.senha_hash},
                )
                senha_ok = amarrado.rowcount == 1
            ok = senha_ok and mfa_ok
            await forca_bruta.registrar(conn, email=dados.email, ip=ip, sucesso=ok)
            if ok:
                sessao = await sessoes.criar(conn, row.id)
                await registrar_login(conn, ator_id=row.id, request_id=rid, ip=ip)
                estado = ("ok", sessao)
            elif senha_ok and not mfa_ok:
                await registrar_login_falha(conn, email_tentado=dados.email, request_id=rid, ip=ip)
                estado = ("mfa",)
            else:
                await registrar_login_falha(conn, email_tentado=dados.email, request_id=rid, ip=ip)
                estado = ("falha",)

    if estado[0] == "ok":
        sessao = estado[1]
        return {"token": sessao.token, "expira_em": para_exibicao(sessao.expira_em).isoformat()}
    if estado[0] == "bloqueado":
        raise ErroAplicacao(
            codigo="muitas_tentativas",
            mensagem="Too many login attempts. Try again later.",
            status=429,
        )
    if estado[0] == "mfa":
        raise ErroAplicacao(
            codigo="mfa_requerido",
            mensagem="Verification code (MFA) required or invalid.",
            status=401,
        )
    raise ErroAplicacao(
        codigo="credenciais_invalidas",
        mensagem="Invalid email or password.",
        status=401,
    )


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, usuario_id: uuid.UUID = Depends(usuario_atual)) -> Response:
    token = extrair_token(request)
    async with transacao() as conn:
        if token:
            await sessoes.revogar(conn, token)
        await registrar_logout(
            conn, ator_id=usuario_id, request_id=request_id_atual(), ip=_ip(request)
        )
    return Response(status_code=204)


@router.get("/auth/me")
async def eu(usuario_id: uuid.UUID = Depends(usuario_atual)) -> dict:
    """Data of the logged-in user (for the sidebar account bar and the account page). No secrets."""
    async with transacao() as conn:
        row = (
            await conn.execute(
                text("SELECT id, nome, email, perfil, mfa_habilitado FROM usuarios WHERE id = :id"),
                {"id": usuario_id},
            )
        ).first()
    if not row:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="User not found.", status=404)
    # `mfa_habilitado`: without it the account page cannot tell whether the 2nd factor is on.
    return {
        "id": str(row.id),
        "nome": row.nome,
        "email": row.email,
        "perfil": row.perfil,
        "mfa_habilitado": bool(row.mfa_habilitado),
    }


@router.post("/auth/senha", status_code=204)
async def trocar_senha(
    dados: TrocarSenhaIn, request: Request, usuario_id: uuid.UUID = Depends(usuario_atual)
) -> Response:
    """Change the user's OWN password: requires the correct current password, validates the policy
    and audits in the same transaction. The plain password is never logged, returned or written
    to the diff (registrar_senha_trocada receives no secret)."""
    validar_politica(dados.nova_senha)
    async with transacao() as conn:
        row = (
            await conn.execute(
                text("SELECT senha_hash FROM usuarios WHERE id = :id"), {"id": usuario_id}
            )
        ).first()
        if not row or not verificar(row.senha_hash, dados.senha_atual):
            raise ErroAplicacao(
                codigo="senha_atual_invalida", mensagem="Incorrect current password.", status=400
            )
        await conn.execute(
            text("UPDATE usuarios SET senha_hash = :h WHERE id = :id"),
            {"h": gerar_hash(dados.nova_senha), "id": usuario_id},
        )
        await registrar_senha_trocada(
            conn, usuario_id=usuario_id, request_id=request_id_atual(), ip=_ip(request)
        )
    return Response(status_code=204)


@router.post("/auth/recuperacao", status_code=202)
async def solicitar_recuperacao(dados: RecuperacaoIn, request: Request) -> Response:
    """Request recovery. The response is ALWAYS generic (202), whether or not the e-mail exists."""
    ip = _ip(request)
    rid = request_id_atual()
    async with transacao() as conn:
        token = await recuperacao.solicitar(conn, dados.email)
        await registrar_recuperacao_iniciada(conn, email_tentado=dados.email, request_id=rid, ip=ip)
    # Delivery happens outside the transaction (token already persisted as a hash); only if the
    # user exists. A channel failure does not change the response: 202 is generic, since a 500
    # here would reveal that the e-mail exists (a nonexistent one never tries to deliver) and
    # become an account-enumeration oracle.
    if token:
        entregar_token(dados.email, token)
    return Response(status_code=202)


@router.post("/auth/recuperacao/confirmar", status_code=204)
async def confirmar_recuperacao(dados: ConfirmarRecuperacaoIn, request: Request) -> Response:
    """Set a new password with a valid (single-use) token. Validates the policy before consuming."""
    async with transacao() as conn:
        usuario_id = await recuperacao.confirmar(conn, dados.token, dados.nova_senha)
        await registrar_senha_trocada(
            conn, usuario_id=usuario_id, request_id=request_id_atual(), ip=_ip(request)
        )
    return Response(status_code=204)


class SsoOidcIn(BaseModel):
    id_token: str


@router.post("/auth/sso/oidc")
async def sso_oidc(dados: SsoOidcIn, request: Request) -> dict:
    """SSO/OIDC login: validate the IdP ID token, provision/sign in the user and create a session."""
    import asyncio

    claims = await asyncio.to_thread(sso.validar_id_token, dados.id_token)
    ip = _ip(request)
    rid = request_id_atual()
    async with transacao() as conn:
        usuario_id = await sso.provisionar(conn, claims, ip=ip, request_id=rid)
        sessao = await sessoes.criar(conn, usuario_id)
        await registrar_login(conn, ator_id=usuario_id, request_id=rid, ip=ip)
    return {"token": sessao.token, "expira_em": para_exibicao(sessao.expira_em).isoformat()}


class MfaAtivarIn(BaseModel):
    codigo: str


class MfaDesativarIn(BaseModel):
    senha_atual: str


@router.post("/auth/mfa/configurar")
async def mfa_configurar(usuario_id: uuid.UUID = Depends(usuario_atual)) -> dict:
    """Start MFA enrolment: generate the secret (stored encrypted in the vault) and return the
    otpauth URI for the authenticator app. MFA only takes effect after /auth/mfa/ativar."""
    async with transacao() as conn:
        segredo, uri = await mfa_fluxo.configurar(conn, usuario_id)
    return {"segredo": segredo, "uri": uri}


@router.post("/auth/mfa/ativar", status_code=204)
async def mfa_ativar(
    dados: MfaAtivarIn, request: Request, usuario_id: uuid.UUID = Depends(usuario_atual)
) -> Response:
    """Confirm MFA by validating a TOTP code; login then requires the 2nd factor."""
    async with transacao() as conn:
        await mfa_fluxo.ativar(conn, usuario_id, dados.codigo)
        await registrar(
            conn,
            acao=Acao.EDITAR,
            recurso_tipo="mfa",
            recurso_id=usuario_id,
            ator_id=usuario_id,
            antes={"mfa": False},
            depois={"mfa": True},
            request_id=request_id_atual(),
            ip=_ip(request),
        )
    return Response(status_code=204)


@router.post("/auth/mfa/desativar", status_code=204)
async def mfa_desativar(
    dados: MfaDesativarIn, request: Request, usuario_id: uuid.UUID = Depends(usuario_atual)
) -> Response:
    """Turn off the user's OWN 2nd factor. Requires the current password; without it a forgotten
    open session could remove the account's second factor. Audited in the same transaction."""
    async with transacao() as conn:
        row = (
            await conn.execute(
                text("SELECT senha_hash FROM usuarios WHERE id = :id"), {"id": usuario_id}
            )
        ).first()
        senha_ok = bool(row) and verificar(row.senha_hash, dados.senha_atual)
        await mfa_fluxo.desativar(conn, usuario_id, senha_ok)
        await registrar(
            conn,
            acao=Acao.EDITAR,
            recurso_tipo="mfa",
            recurso_id=usuario_id,
            ator_id=usuario_id,
            antes={"mfa": True},
            depois={"mfa": False},
            request_id=request_id_atual(),
            ip=_ip(request),
        )
    return Response(status_code=204)
