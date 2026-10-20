"""No secret (password/hash/plain recovery token) in any response or metadata."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from cflow_core.auth import notificacao
from cflow_core.auth.notificacao import ProvedorMemoria

from ._arnes_auth import semear_usuario

pytestmark = pytest.mark.integration

SENHA = "long-secret-password-9"


@pytest.fixture
def canal():
    mem = ProvedorMemoria()
    notificacao.definir_provedor(mem)
    yield mem
    notificacao.definir_provedor(None)


async def test_senha_e_token_nao_vazam(owner_engine, app_engine, cliente, canal):
    await semear_usuario(owner_engine, email="seg@example.com", senha=SENHA)

    # login (success): the body contains neither the password nor a hash
    login = await cliente.post("/auth/login", json={"email": "seg@example.com", "senha": SENHA})
    assert SENHA not in login.text

    # recovery: the plain token only goes through the channel, never the response
    rec = await cliente.post("/auth/recuperacao", json={"email": "seg@example.com"})
    _, token = canal.enviados[0]
    assert token not in rec.text
    assert SENHA not in rec.text

    # metadata: the plain password and the plain token are not in the database
    async with app_engine.connect() as conn:
        aud = (
            await conn.execute(text("SELECT string_agg(auditoria::text, ' ') FROM auditoria"))
        ).scalar_one() or ""
        usuarios = (
            await conn.execute(text("SELECT string_agg(usuarios::text, ' ') FROM usuarios"))
        ).scalar_one() or ""
        recs = (
            await conn.execute(
                text("SELECT string_agg(tokens_recuperacao::text, ' ') FROM tokens_recuperacao")
            )
        ).scalar_one() or ""
    for dump in (aud, usuarios, recs):
        assert SENHA not in dump
        assert token not in dump
