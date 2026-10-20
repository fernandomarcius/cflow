"""Logout revokes the session immediately; revocation is idempotent."""

from __future__ import annotations

import pytest

from cflow_core.auth import sessoes

from ._arnes_auth import criar_sessao, semear_usuario

pytestmark = pytest.mark.integration


async def test_logout_revoga_e_invalida_sessao(owner_engine, app_engine, cliente):
    uid = await semear_usuario(owner_engine, email="lo@example.com")
    token = await criar_sessao(app_engine, uid)

    resp = await cliente.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 204

    async with app_engine.begin() as conn:
        assert await sessoes.validar(conn, token) is None


async def test_revogar_e_idempotente(owner_engine, app_engine):
    uid = await semear_usuario(owner_engine, email="lo2@example.com")
    token = await criar_sessao(app_engine, uid)
    async with app_engine.begin() as conn:
        await sessoes.revogar(conn, token)
        await sessoes.revogar(conn, token)  # the second time does not fail
        assert await sessoes.validar(conn, token) is None
