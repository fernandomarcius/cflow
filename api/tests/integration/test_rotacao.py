"""Credential rotation keeping the reference."""

from __future__ import annotations

import pytest

from ._arnes_auth import CONFIG_PG, SENHA_APP, usuario_logado

pytestmark = pytest.mark.integration

CONFIG = CONFIG_PG


async def test_rotacionar_mantem_referencia(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="adr5@example.com", perfil="admin")
    cid = (
        await cliente.post(
            "/conexoes",
            json={
                "nome": "pg-rot",
                "tipo": "postgres",
                "config": CONFIG,
                "segredo": "wrong-password",
            },
            headers=h,
        )
    ).json()["id"]

    # with a wrong password, the test fails
    assert (await cliente.post(f"/conexoes/{cid}/testar", headers=h)).json()["ok"] is False

    # rotate to the correct password (same connection, without editing it)
    r = await cliente.post(
        f"/conexoes/{cid}/rotacionar-credencial",
        json={"novo_segredo": SENHA_APP},
        headers=h,
    )
    assert r.status_code == 204

    # now the test passes, without having edited the connection
    assert (await cliente.post(f"/conexoes/{cid}/testar", headers=h)).json()["ok"] is True


async def test_rotacao_so_admin(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esprot@example.com", perfil="especialista"
    )
    cid = (
        await cliente.post(
            "/conexoes",
            json={"nome": "pg-x", "tipo": "postgres", "config": CONFIG, "segredo": "s"},
            headers=h,
        )
    ).json()["id"]
    r = await cliente.post(
        f"/conexoes/{cid}/rotacionar-credencial", json={"novo_segredo": "y"}, headers=h
    )
    assert r.status_code == 403  # a specialist cannot rotate a credential
