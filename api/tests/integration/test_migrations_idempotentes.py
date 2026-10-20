"""Idempotent migrations."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from .conftest import aplicar_migrations

pytestmark = pytest.mark.integration


def test_aplicar_migrations_duas_vezes_nao_quebra():
    # `_migrado` already applied them once; applying again must be a no-op without errors.
    aplicar_migrations()
    aplicar_migrations()


async def test_schema_base_presente(owner_engine):
    async with owner_engine.connect() as conn:
        existe = await conn.execute(text("SELECT to_regclass('public.auditoria') IS NOT NULL"))
        assert existe.scalar_one() is True
        tem_ator_tipo = await conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name='auditoria' AND column_name='ator_tipo')"
            )
        )
        assert tem_ator_tipo.scalar_one() is True
