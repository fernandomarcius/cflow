"""Connection authorization at the edge."""

from __future__ import annotations

import pytest

from ._arnes_auth import CONFIG_PG, usuario_logado

pytestmark = pytest.mark.integration

CONFIG = CONFIG_PG


async def test_operador_negado(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="opcx@example.com", perfil="operador")
    assert (await cliente.get("/conexoes", headers=h)).status_code == 403
    assert (
        await cliente.post(
            "/conexoes", json={"nome": "n", "tipo": "postgres", "config": CONFIG}, headers=h
        )
    ).status_code == 403


async def test_sem_sessao_401(app_engine, cliente):
    assert (await cliente.get("/conexoes")).status_code == 401
