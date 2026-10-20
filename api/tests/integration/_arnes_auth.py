"""Test harness for authentication: seed a user with a password (hash), create a session.

Also exposes `CONFIG_PG` / `SENHA_APP`: connection details of the test Postgres (host, port, role
and password derived from `CFLOW_DATABASE_URL`; database = the isolated test database), for tests
that register a Postgres connection as a data source.
"""

from __future__ import annotations

import os
import uuid

from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine

from cflow_core.auth import sessoes
from cflow_core.auth.senhas import gerar_hash

from .conftest import DB_TESTE


def _pg_da_suite() -> tuple[dict, str]:
    url = make_url(
        os.environ.get(
            "CFLOW_DATABASE_URL", "postgresql+asyncpg://cflow_app:x@localhost:5432/cflow"
        )
    )
    config = {
        "host": url.host or "localhost",
        "porta": url.port or 5432,
        # The suite redirects everything to the isolated test database; that is where the
        # migrated tables live, so connections registered by tests must point at it.
        "base": DB_TESTE,
        "usuario": url.username or "cflow_app",
    }
    return config, url.password or ""


_CONFIG, SENHA_APP = _pg_da_suite()
#: Connection config pointing at the suite's Postgres (registered in tests as a Postgres data source).
CONFIG_PG: dict = dict(_CONFIG)


async def semear_usuario(
    owner_engine: AsyncEngine,
    *,
    email: str,
    senha: str | None = None,
    ativo: bool = True,
    perfil: str = "operador",
) -> uuid.UUID:
    """Create a user (through the owner role) with an optional password (stored as a hash)."""
    senha_hash = gerar_hash(senha) if senha else None
    async with owner_engine.begin() as conn:
        res = await conn.execute(
            text(
                "INSERT INTO usuarios (email, nome, perfil, ativo, senha_hash) "
                "VALUES (:e, 'Teste', :p, :a, :h) RETURNING id"
            ),
            {"e": email, "p": perfil, "a": ativo, "h": senha_hash},
        )
        return res.scalar_one()


async def criar_sessao(app_engine: AsyncEngine, usuario_id: uuid.UUID) -> str:
    """Create a session and return the plain token."""
    async with app_engine.begin() as conn:
        sessao = await sessoes.criar(conn, usuario_id)
    return sessao.token


async def login_token(cliente, email: str, senha: str) -> str:
    """Log in through the API and return the session token (Bearer)."""
    r = await cliente.post("/auth/login", json={"email": email, "senha": senha})
    assert r.status_code == 200, r.text
    return r.json()["token"]


async def usuario_logado(
    owner_engine: AsyncEngine,
    cliente,
    *,
    email: str,
    perfil: str,
    senha: str = "long-test-password-1",
) -> tuple[uuid.UUID, dict]:
    """Seed a user with the given profile, log in and return (id, Authorization headers)."""
    uid = await semear_usuario(owner_engine, email=email, senha=senha, perfil=perfil)
    token = await login_token(cliente, email, senha)
    return uid, {"Authorization": f"Bearer {token}"}
