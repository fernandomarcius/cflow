"""Vault: store/resolve/rotate; the secret is only ever encrypted."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from cflow_core.vault import service as vault

pytestmark = pytest.mark.integration


async def test_guardar_resolver_rotacionar(app_engine):
    async with app_engine.begin() as conn:
        ref = await vault.guardar(conn, "super-secret-value")
        assert await vault.resolver(conn, ref) == "super-secret-value"
        await vault.rotacionar(conn, ref, "new-secret")
        assert await vault.resolver(conn, ref) == "new-secret"


async def test_cofre_guarda_cifrado(app_engine):
    async with app_engine.begin() as conn:
        ref = await vault.guardar(conn, "plain-text-xyz")
    async with app_engine.connect() as conn:
        cif = (
            await conn.execute(
                text("SELECT segredo_cifrado FROM cofre_credenciais WHERE id = :id"), {"id": ref}
            )
        ).scalar_one()
    assert b"plain-text-xyz" not in bytes(cif)  # it is not in clear text
