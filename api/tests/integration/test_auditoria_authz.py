"""Authorization: only Auditor/Admin can query/export."""

from __future__ import annotations

import pytest

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


async def test_operador_negado(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="opaud@example.com", perfil="operador")
    assert (await cliente.get("/auditoria", headers=h)).status_code == 403
    assert (await cliente.get("/auditoria/exportacao?formato=csv", headers=h)).status_code == 403


async def test_auditor_permitido(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="aok@example.com", perfil="auditor")
    assert (await cliente.get("/auditoria", headers=h)).status_code == 200


async def test_sem_sessao_401(app_engine, cliente):
    assert (await cliente.get("/auditoria")).status_code == 401
