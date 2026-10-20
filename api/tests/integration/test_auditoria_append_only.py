"""Append-only enforced by the database.

Inserts a row as cflow_owner (allowed) and proves that cflow_app can NOT alter or delete the
trail: the database denies it by permission.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

pytestmark = pytest.mark.integration


async def _semear_registro(owner_engine) -> int:
    async with owner_engine.begin() as conn:
        res = await conn.execute(
            text(
                "INSERT INTO auditoria (ator_tipo, acao, recurso_tipo) "
                "VALUES ('sistema', 'criar', 'recurso_teste') RETURNING id"
            )
        )
        return res.scalar_one()


async def test_app_nao_pode_update_auditoria(owner_engine, app_engine):
    id_ = await _semear_registro(owner_engine)
    with pytest.raises(ProgrammingError) as exc:
        async with app_engine.begin() as conn:
            await conn.execute(
                text("UPDATE auditoria SET acao = 'adulterado' WHERE id = :i"), {"i": id_}
            )
    assert "permission denied" in str(exc.value).lower()


async def test_app_nao_pode_delete_auditoria(owner_engine, app_engine):
    id_ = await _semear_registro(owner_engine)
    with pytest.raises(ProgrammingError) as exc:
        async with app_engine.begin() as conn:
            await conn.execute(text("DELETE FROM auditoria WHERE id = :i"), {"i": id_})
    assert "permission denied" in str(exc.value).lower()


async def test_app_pode_inserir_e_ler(app_engine):
    async with app_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO auditoria (ator_tipo, acao, recurso_tipo) "
                "VALUES ('sistema', 'criar', 'recurso_teste')"
            )
        )
    async with app_engine.connect() as conn:
        total = await conn.execute(text("SELECT count(*) FROM auditoria"))
        assert total.scalar_one() == 1
