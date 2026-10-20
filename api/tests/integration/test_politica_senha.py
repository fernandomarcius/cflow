"""Password policy enforced on reset."""

from __future__ import annotations

import pytest

from cflow_core.auth import notificacao
from cflow_core.auth.notificacao import ProvedorMemoria

from ._arnes_auth import semear_usuario

pytestmark = pytest.mark.integration


@pytest.fixture
def canal():
    mem = ProvedorMemoria()
    notificacao.definir_provedor(mem)
    yield mem
    notificacao.definir_provedor(None)


async def test_senha_curta_rejeitada_e_token_preservado(owner_engine, app_engine, cliente, canal):
    await semear_usuario(owner_engine, email="pol@example.com", senha="old-password-long-enough-1")
    await cliente.post("/auth/recuperacao", json={"email": "pol@example.com"})
    _, token = canal.enviados[0]

    # fewer than 12 characters -> rejected
    curta = await cliente.post(
        "/auth/recuperacao/confirmar", json={"token": token, "nova_senha": "curta"}
    )
    assert curta.status_code == 400
    assert curta.json()["erro"]["codigo"] == "senha_fraca"

    # the token was not consumed: a valid password still works
    ok = await cliente.post(
        "/auth/recuperacao/confirmar",
        json={"token": token, "nova_senha": "valid-password-long-enough-2"},
    )
    assert ok.status_code == 204
