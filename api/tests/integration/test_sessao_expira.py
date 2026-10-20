"""A session expires by inactivity and by absolute time."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from cflow_core.auth import sessoes
from cflow_core.config import get_settings

from ._arnes_auth import criar_sessao, semear_usuario

pytestmark = pytest.mark.integration


async def test_sessao_fresca_e_valida(owner_engine, app_engine):
    uid = await semear_usuario(owner_engine, email="s1@example.com")
    token = await criar_sessao(app_engine, uid)
    async with app_engine.begin() as conn:
        assert await sessoes.validar(conn, token) == uid


async def test_expira_por_inatividade(owner_engine, app_engine):
    uid = await semear_usuario(owner_engine, email="s2@example.com")
    token = await criar_sessao(app_engine, uid)
    # Age the last activity past the CONFIGURED limit (+1 min of slack). A hand-written number
    # would break the test whenever the window changed, with nothing in the mechanics changing.
    limite = get_settings().sessao_inatividade_segundos
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessoes SET ultima_atividade_em = now() - make_interval(secs => :s)"),
            {"s": limite + 60},
        )
    async with app_engine.begin() as conn:
        assert await sessoes.validar(conn, token) is None


async def test_expira_por_tempo_absoluto(owner_engine, app_engine):
    uid = await semear_usuario(owner_engine, email="s3@example.com")
    token = await criar_sessao(app_engine, uid)
    async with owner_engine.begin() as conn:
        await conn.execute(text("UPDATE sessoes SET expira_em = now() - interval '1 minute'"))
    async with app_engine.begin() as conn:
        assert await sessoes.validar(conn, token) is None
