"""List users with profile/state/last access."""

from __future__ import annotations

import pytest

from ._arnes_auth import semear_usuario, usuario_logado

pytestmark = pytest.mark.integration


async def test_lista_exibe_perfil_estado_e_ultimo_acesso(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="al@example.com", perfil="admin")
    await semear_usuario(owner_engine, email="semlogin@example.com", perfil="operador")

    r = await cliente.get("/usuarios", headers=h)
    assert r.status_code == 200
    lista = r.json()
    por_email = {u["email"]: u for u in lista}
    assert por_email["semlogin@example.com"]["ultimo_acesso"] is None
    assert por_email["semlogin@example.com"]["perfil"] == "operador"
    assert por_email["semlogin@example.com"]["ativo"] is True
