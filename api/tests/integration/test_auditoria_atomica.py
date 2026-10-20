"""Audit is atomic with the write: a rollback leaves no orphan record."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ._arnes_escrita import escrever_recurso_exemplo

pytestmark = pytest.mark.integration


async def test_escrita_revertida_nao_deixa_orfao(app_engine):
    with pytest.raises(RuntimeError):
        await escrever_recurso_exemplo(app_engine, falhar=True)

    async with app_engine.connect() as conn:
        total = await conn.execute(text("SELECT count(*) FROM auditoria"))
        assert total.scalar_one() == 0
