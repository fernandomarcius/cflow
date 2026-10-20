"""Real binding of runtime parameters in the source (postgres connector against cflow_test).

Proves end to end that `:nome` is bound natively (it really filters) and that a malicious value
enters as a LITERAL (no SQL injection). Uses the test database's own cflow_app credential.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import make_url

from cflow_core.conexoes import service

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

# SQL with no table dependency: 3 fixed values, filtered by the :alvo parameter.
SQL = "SELECT v FROM (VALUES ('a'),('b'),('c')) t(v) WHERE v = :alvo"


async def _criar_conexao_pg(cliente, h) -> str:
    u = make_url(
        os.environ["CFLOW_DATABASE_URL"]
    )  # cflow_app -> cflow_test (redirected by conftest)
    r = await cliente.post(
        "/conexoes",
        json={
            "nome": "pg-param-test",
            "tipo": "postgres",
            "config": {"host": u.host, "porta": u.port, "base": u.database, "usuario": u.username},
            "segredo": u.password,
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_param_filtra_de_verdade(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="pp1@example.com", perfil="especialista"
    )
    cid = await _criar_conexao_pg(cliente, h)
    import uuid

    cols, linhas = await service.amostra(uuid.UUID(cid), SQL, None, {"alvo": "b"})
    assert cols == ["v"]
    assert linhas == [{"v": "b"}]


async def test_valor_de_injecao_e_literal_nao_injeta(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="pp2@example.com", perfil="especialista"
    )
    cid = await _criar_conexao_pg(cliente, h)
    import uuid

    # Classic injection payload: if concatenated, it would return all 3 rows.
    payload = "b' OR '1'='1"
    cols, linhas = await service.amostra(uuid.UUID(cid), SQL, None, {"alvo": payload})
    assert linhas == []  # compared as a literal -> matches no row
    assert cols == ["v"]  # columns known even with 0 rows (result schema)
