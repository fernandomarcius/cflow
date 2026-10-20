"""API tokens for SERVICE accounts: the identity of an integrator, with no password.

An integration that signs in with e-mail and password is a poor fit: the session expires (24 h
absolute, 8 h idle), re-login becomes a loop against the brute-force-protected route, and the
account password ends up in a config file. Here:

- **Only `servico` accounts get tokens**, and they sign in only by token (password login is
  refused). A token is not a stretched human session: people have sessions, MFA and short expiry.
- **Opaque, with a prefix** `cfs_`: the prefix tells `usuario_atual` it is an API token and makes
  the secret recognisable to leak scanners. Only the SHA-256 (the token already has 256 bits of
  entropy) and the `prefixo` are stored - the first characters, so the UI can say WHICH token
  without revealing it.
- **Always expires** (1 to 365 days) and is **revocable**. A suspended account, or one that is no
  longer `servico`, invalidates all its tokens immediately - validation checks both on every use.
- **Shown once**, at issuance. Issuance and revocation go to the audit trail, never the secret.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..audit import Acao, registrar
from ..authz.perfis import Perfil
from ..errors import ErroAplicacao
from ..timezone import agora_utc

PREFIXO = "cfs_"
DIAS_PADRAO = 90
DIAS_MAX = 365
# Writing `ultimo_uso_em` on every request would be one write per poll (an integration may poll an
# execution every few seconds). Once a minute is enough for the UI to say "used recently".
_INTERVALO_USO = timedelta(seconds=60)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def eh_token_api(token: str | None) -> bool:
    return bool(token) and token.startswith(PREFIXO)


@dataclass
class TokenEmitido:
    id: uuid.UUID
    token: str  # clear text - only here, once
    prefixo: str
    expira_em: datetime


async def _exigir_conta_servico(conn: AsyncConnection, usuario_id: uuid.UUID) -> None:
    row = (
        await conn.execute(
            text("SELECT perfil, ativo FROM usuarios WHERE id = :u"), {"u": usuario_id}
        )
    ).first()
    if row is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="User not found.", status=404)
    if row.perfil != Perfil.SERVICO:
        raise ErroAplicacao(
            codigo="token_so_para_servico",
            mensagem="API tokens are for service accounts only - people sign in with a password (and MFA).",
            status=409,
        )
    if not row.ativo:
        raise ErroAplicacao(
            codigo="conta_suspensa",
            mensagem="The account is suspended: restore access before issuing a token.",
            status=409,
        )


async def emitir(
    conn: AsyncConnection,
    *,
    usuario_id: uuid.UUID,
    nome: str,
    dias: int = DIAS_PADRAO,
    ator_id: uuid.UUID,
    ip: str | None = None,
    request_id: str | None = None,
) -> TokenEmitido:
    """Issue a token for the service account. The clear-text value only exists in the return value."""
    rotulo = (nome or "").strip()
    if not rotulo:
        raise ErroAplicacao(
            codigo="nome_invalido",
            mensagem="Give the token a name - where it will live (e.g. 'integration adapter - staging').",
            status=400,
        )
    if not isinstance(dias, int) or isinstance(dias, bool) or not 1 <= dias <= DIAS_MAX:
        raise ErroAplicacao(
            codigo="validade_invalida",
            mensagem=f"Token validity ranges from 1 to {DIAS_MAX} days.",
            status=400,
        )
    await _exigir_conta_servico(conn, usuario_id)
    token = PREFIXO + secrets.token_urlsafe(32)
    prefixo = token[: len(PREFIXO) + 6]
    expira = agora_utc() + timedelta(days=dias)
    tid = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO tokens_api (id, usuario_id, nome, prefixo, token_hash, criado_por, "
            "expira_em) VALUES (:id, :u, :n, :p, :h, :por, :exp)"
        ),
        {
            "id": tid,
            "u": usuario_id,
            "n": rotulo,
            "p": prefixo,
            "h": _hash(token),
            "por": ator_id,
            "exp": expira,
        },
    )
    await registrar(
        conn,
        acao=Acao.CRIAR,
        recurso_tipo="token_api",
        recurso_id=tid,
        ator_id=ator_id,
        antes=None,
        # WITHOUT the token or its hash: name, prefix and expiry identify it.
        depois={
            "usuario_id": str(usuario_id),
            "nome": rotulo,
            "prefixo": prefixo,
            "expira_em": expira.isoformat(),
        },
        request_id=request_id,
        ip=ip,
    )
    return TokenEmitido(id=tid, token=token, prefixo=prefixo, expira_em=expira)


async def listar(conn: AsyncConnection, usuario_id: uuid.UUID) -> list[dict]:
    """The account's tokens, newest first - no secret and no hash."""
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT id, nome, prefixo, criado_em, expira_em, ultimo_uso_em, revogado_em "
                    "FROM tokens_api WHERE usuario_id = :u ORDER BY criado_em DESC"
                ),
                {"u": usuario_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def revogar(
    conn: AsyncConnection,
    *,
    usuario_id: uuid.UUID,
    token_id: uuid.UUID,
    ator_id: uuid.UUID,
    ip: str | None = None,
    request_id: str | None = None,
) -> None:
    """Revoke (idempotent: an already revoked token is not written to the trail again). 404 if it is not this account's."""
    row = (
        await conn.execute(
            text(
                "SELECT nome, prefixo, revogado_em FROM tokens_api "
                "WHERE id = :t AND usuario_id = :u"
            ),
            {"t": token_id, "u": usuario_id},
        )
    ).first()
    if row is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Token not found.", status=404)
    if row.revogado_em is not None:
        return
    await conn.execute(
        text("UPDATE tokens_api SET revogado_em = now(), revogado_por = :a WHERE id = :t"),
        {"a": ator_id, "t": token_id},
    )
    await registrar(
        conn,
        acao=Acao.EXCLUIR,
        recurso_tipo="token_api",
        recurso_id=token_id,
        ator_id=ator_id,
        antes={"nome": row.nome, "prefixo": row.prefixo},
        depois={"revogado": True},
        request_id=request_id,
        ip=ip,
    )


async def validar(conn: AsyncConnection, token: str) -> uuid.UUID | None:
    """The owner of a valid token: existing, not revoked, not expired, from an ACTIVE account that is
    still `servico`. None in any other case - the 401 the edge returns is generic."""
    if not eh_token_api(token):
        return None
    row = (
        await conn.execute(
            text(
                "SELECT t.id, t.usuario_id, t.ultimo_uso_em FROM tokens_api t "
                "JOIN usuarios u ON u.id = t.usuario_id "
                "WHERE t.token_hash = :h AND t.revogado_em IS NULL AND t.expira_em > now() "
                # `::text`: the `servico` label only exists in the enum after the migration that adds it.
                "AND u.ativo AND u.perfil::text = 'servico'"
            ),
            {"h": _hash(token)},
        )
    ).first()
    if row is None:
        return None
    agora = agora_utc()
    if row.ultimo_uso_em is None or agora - row.ultimo_uso_em > _INTERVALO_USO:
        await conn.execute(
            text("UPDATE tokens_api SET ultimo_uso_em = :a WHERE id = :t"),
            {"a": agora, "t": row.id},
        )
    return row.usuario_id
