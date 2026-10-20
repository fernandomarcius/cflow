"""Log retention is separate from the permanent trail."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from cflow_core.auditoria import retencao

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


async def _conta_trilha(app_engine) -> int:
    async with app_engine.connect() as conn:
        return (await conn.execute(text("SELECT count(*) FROM auditoria"))).scalar_one()


async def test_expurgo_nao_afeta_trilha(owner_engine, app_engine, cliente):
    await usuario_logado(owner_engine, cliente, email="adr@example.com", perfil="admin")
    antes = await _conta_trilha(app_engine)
    async with app_engine.begin() as conn:
        removidos = await retencao.expurgar_logs_tecnicos(conn, dias=1)
    assert removidos == 0
    assert await _conta_trilha(app_engine) == antes  # trail intact


async def test_alterar_politica_auditada(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="adr2@example.com", perfil="admin")
    r = await cliente.patch("/admin/retencao-logs", json={"dias": 30}, headers=h)
    assert r.status_code == 204
    async with app_engine.connect() as conn:
        valor = (
            await conn.execute(
                text("SELECT valor FROM configuracoes WHERE chave='log_retencao_dias'")
            )
        ).scalar_one()
        assert valor == "30"
        n = (
            await conn.execute(
                text("SELECT count(*) FROM auditoria WHERE acao='retencao_alterada'")
            )
        ).scalar_one()
        assert n == 1


async def test_nao_admin_negado(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="adr3@example.com", perfil="auditor")
    assert (
        await cliente.patch("/admin/retencao-logs", json={"dias": 10}, headers=h)
    ).status_code == 403
