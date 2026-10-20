"""Authorization at the edge."""

from __future__ import annotations

import pytest

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


async def test_nao_admin_negado_em_rota_admin(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="opb@example.com", perfil="operador")
    assert (await cliente.get("/usuarios", headers=h)).status_code == 403


async def test_admin_permitido(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="adb@example.com", perfil="admin")
    assert (await cliente.get("/usuarios", headers=h)).status_code == 200


async def test_sem_sessao_401(app_engine, cliente):
    assert (await cliente.get("/usuarios")).status_code == 401
