"""Authorization of the pipeline editor at the edge."""

from __future__ import annotations

import pytest

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


async def test_operador_negado(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="oppipe@example.com", perfil="operador"
    )
    assert (await cliente.post("/pipelines", json={"nome": "x"}, headers=h)).status_code == 403


async def test_sem_sessao_401(app_engine, cliente):
    assert (await cliente.post("/pipelines", json={"nome": "x"})).status_code == 401
