"""OIDC SSO: validates an ID token (RS256/JWKS) against an IdP (Keycloak or any OIDC provider) and
provisions/logs in the local user. No IdP secret is embedded - only the public issuer/client_id/JWKS.

Validation (PyJWKClient + jwt.decode) is synchronous; the route runs it in a thread.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..audit import Acao, AtorTipo, registrar
from ..config import get_settings
from ..errors import ErroAplicacao


def _jwks_uri() -> str:
    cfg = get_settings()
    if cfg.oidc_jwks_uri:
        return cfg.oidc_jwks_uri
    if not cfg.oidc_issuer:
        raise ErroAplicacao(
            codigo="sso_indisponivel", mensagem="SSO is not configured.", status=503
        )
    import httpx

    disc = httpx.get(
        f"{cfg.oidc_issuer.rstrip('/')}/.well-known/openid-configuration", timeout=5
    ).json()
    return disc["jwks_uri"]


def validar_id_token(id_token: str) -> dict:
    """Validate signature (RS256 via JWKS), issuer and audience; return the claims. Synchronous."""
    import jwt

    cfg = get_settings()
    if not cfg.oidc_issuer or not cfg.oidc_client_id:
        raise ErroAplicacao(
            codigo="sso_indisponivel", mensagem="SSO is not configured.", status=503
        )
    try:
        cliente = jwt.PyJWKClient(_jwks_uri())
        chave = cliente.get_signing_key_from_jwt(id_token)
        return jwt.decode(
            id_token,
            chave.key,
            algorithms=["RS256"],
            audience=cfg.oidc_client_id,
            issuer=cfg.oidc_issuer,
        )
    except ErroAplicacao:
        raise
    except Exception as exc:  # noqa: BLE001 - invalid token, no details
        raise ErroAplicacao(
            codigo="sso_invalido", mensagem="Invalid SSO token.", status=401
        ) from exc


async def provisionar(
    conn: AsyncConnection, claims: dict, *, ip: str | None = None, request_id: str | None = None
) -> uuid.UUID:
    """Look up the user by the token's e-mail; if absent, provision it (default profile, oidc source).
    Inactive users are refused. Returns the id."""
    email = claims.get("email")
    if not email:
        raise ErroAplicacao(codigo="sso_sem_email", mensagem="SSO token has no e-mail.", status=400)

    row = (
        await conn.execute(text("SELECT id, ativo FROM usuarios WHERE email = :e"), {"e": email})
    ).first()
    if row is not None:
        if not row.ativo:
            raise ErroAplicacao(codigo="usuario_inativo", mensagem="Inactive user.", status=403)
        return row.id

    novo = uuid.uuid4()
    nome = claims.get("name") or claims.get("preferred_username") or email
    await conn.execute(
        text(
            "INSERT INTO usuarios (id, email, nome, perfil, fonte_auth, ativo) "
            "VALUES (:id, :e, :n, :p, 'oidc', true)"
        ),
        {"id": novo, "e": email, "n": nome, "p": get_settings().oidc_perfil_padrao},
    )
    await registrar(
        conn,
        acao=Acao.CRIAR,
        recurso_tipo="usuario",
        recurso_id=novo,
        ator_tipo=AtorTipo.SISTEMA,
        antes=None,
        depois={"email": email, "fonte_auth": "oidc"},
        request_id=request_id,
        ip=ip,
    )
    return novo
