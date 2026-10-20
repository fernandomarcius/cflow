"""Login."""

from __future__ import annotations

import pytest

from ._arnes_auth import semear_usuario

pytestmark = pytest.mark.integration

SENHA = "long-enough-password-123"


async def test_login_sucesso(owner_engine, app_engine, cliente):
    await semear_usuario(owner_engine, email="ok@example.com", senha=SENHA)
    resp = await cliente.post("/auth/login", json={"email": "ok@example.com", "senha": SENHA})
    assert resp.status_code == 200
    corpo = resp.json()
    assert corpo["token"]
    assert corpo["expira_em"]


async def test_senha_errada_e_email_inexistente_indistinguiveis(owner_engine, app_engine, cliente):
    await semear_usuario(owner_engine, email="existe@example.com", senha=SENHA)

    r_senha = await cliente.post(
        "/auth/login", json={"email": "existe@example.com", "senha": "wrong-but-long-1"}
    )
    r_inex = await cliente.post(
        "/auth/login", json={"email": "naoexiste@example.com", "senha": "wrong-but-long-1"}
    )
    assert r_senha.status_code == r_inex.status_code == 401
    # identical bodies (same code and message), so the e-mail's existence does not leak
    assert r_senha.json()["erro"]["codigo"] == r_inex.json()["erro"]["codigo"]
    assert r_senha.json()["erro"]["mensagem"] == r_inex.json()["erro"]["mensagem"]


async def test_usuario_inativo_recusado(owner_engine, app_engine, cliente):
    await semear_usuario(owner_engine, email="inativo@example.com", senha=SENHA, ativo=False)
    resp = await cliente.post("/auth/login", json={"email": "inativo@example.com", "senha": SENHA})
    assert resp.status_code == 401
