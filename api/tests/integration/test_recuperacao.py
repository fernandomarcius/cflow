"""Password recovery by token."""

from __future__ import annotations

import pytest

from cflow_core.auth import notificacao
from cflow_core.auth.notificacao import ProvedorMemoria

from ._arnes_auth import semear_usuario

pytestmark = pytest.mark.integration

SENHA = "old-password-long-enough-1"
NOVA = "new-password-long-enough-2"


@pytest.fixture
def canal():
    mem = ProvedorMemoria()
    notificacao.definir_provedor(mem)
    yield mem
    notificacao.definir_provedor(None)


async def test_fluxo_feliz_troca_senha(owner_engine, app_engine, cliente, canal):
    await semear_usuario(owner_engine, email="rec@example.com", senha=SENHA)

    r = await cliente.post("/auth/recuperacao", json={"email": "rec@example.com"})
    assert r.status_code == 202
    assert len(canal.enviados) == 1
    _, token = canal.enviados[0]

    r2 = await cliente.post(
        "/auth/recuperacao/confirmar", json={"token": token, "nova_senha": NOVA}
    )
    assert r2.status_code == 204

    # the new password logs in; the old one fails
    assert (
        await cliente.post("/auth/login", json={"email": "rec@example.com", "senha": NOVA})
    ).status_code == 200
    assert (
        await cliente.post("/auth/login", json={"email": "rec@example.com", "senha": SENHA})
    ).status_code == 401


async def test_token_usado_e_rejeitado_na_segunda_vez(owner_engine, app_engine, cliente, canal):
    await semear_usuario(owner_engine, email="rec2@example.com", senha=SENHA)
    await cliente.post("/auth/recuperacao", json={"email": "rec2@example.com"})
    _, token = canal.enviados[0]

    assert (
        await cliente.post("/auth/recuperacao/confirmar", json={"token": token, "nova_senha": NOVA})
    ).status_code == 204
    # reusing the same token -> rejected
    assert (
        await cliente.post("/auth/recuperacao/confirmar", json={"token": token, "nova_senha": NOVA})
    ).status_code == 400


async def test_token_invalido_rejeitado(app_engine, cliente, canal):
    r = await cliente.post(
        "/auth/recuperacao/confirmar", json={"token": "nonexistent", "nova_senha": NOVA}
    )
    assert r.status_code == 400


async def test_email_inexistente_resposta_generica(app_engine, cliente, canal):
    r = await cliente.post("/auth/recuperacao", json={"email": "ninguem@example.com"})
    assert r.status_code == 202  # generic
    assert canal.enviados == []  # nothing delivered
