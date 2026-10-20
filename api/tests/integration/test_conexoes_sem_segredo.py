"""A credential secret does not leak into responses, metadata or the audit trail."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ._arnes_auth import CONFIG_PG, usuario_logado

pytestmark = pytest.mark.integration

SEGREDO = "ultra-secret-database-password"
CONFIG = CONFIG_PG


async def test_segredo_nunca_vaza(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esps@example.com", perfil="especialista"
    )
    r = await cliente.post(
        "/conexoes",
        json={"nome": "pg-seg", "tipo": "postgres", "config": CONFIG, "segredo": SEGREDO},
        headers=h,
    )
    assert r.status_code == 201
    assert SEGREDO not in r.text

    async with app_engine.connect() as conn:
        conex = (
            await conn.execute(text("SELECT string_agg(conexoes::text, ' ') FROM conexoes"))
        ).scalar_one() or ""
        aud = (
            await conn.execute(text("SELECT string_agg(auditoria::text, ' ') FROM auditoria"))
        ).scalar_one() or ""
    assert SEGREDO not in conex  # connection metadata has no secret
    assert SEGREDO not in aud  # audit diff has no secret
