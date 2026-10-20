"""Test a connection and browse its schema."""

from __future__ import annotations

import pytest

from ._arnes_auth import CONFIG_PG, SENHA_APP, usuario_logado

pytestmark = pytest.mark.integration

CONFIG = CONFIG_PG


async def _criar(cliente, h, nome, segredo):
    return await cliente.post(
        "/conexoes",
        json={"nome": nome, "tipo": "postgres", "config": CONFIG, "segredo": segredo},
        headers=h,
    )


async def test_testar_ok_e_navegar(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="espt@example.com", perfil="especialista"
    )
    cid = (await _criar(cliente, h, "pg-ok", SENHA_APP)).json()["id"]

    t = await cliente.post(f"/conexoes/{cid}/testar", headers=h)
    assert t.status_code == 200 and t.json()["ok"] is True

    s = await cliente.get(f"/conexoes/{cid}/schema", headers=h)
    assert s.status_code == 200
    public = next((g for g in s.json()["schemas"] if g["schema"] == "public"), None)
    assert public is not None and "auditoria" in public["tabelas"]


async def test_testar_senha_errada_causa_saneada(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="espt2@example.com", perfil="especialista"
    )
    cid = (await _criar(cliente, h, "pg-ruim", "wrong-password")).json()["id"]
    t = await cliente.post(f"/conexoes/{cid}/testar", headers=h)
    assert t.status_code == 200
    assert t.json()["ok"] is False
    assert t.json()["causa"] == "authentication refused"
    assert "wrong-password" not in t.text  # sanitised cause, no echo of the secret
