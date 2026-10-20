"""SQL Server connector (python-tds). Sanitized errors are always testable; the happy path runs
against a real SQL Server (docker-compose --profile mssql on localhost:11433) and is skipped when
unavailable.
"""

from __future__ import annotations

import socket
import uuid

import pytest

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

MSSQL_HOST = "localhost"
MSSQL_PORT = 11433
MSSQL_SENHA = "Change_Me_2026!"


def _mssql_no_ar() -> bool:
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect((MSSQL_HOST, MSSQL_PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


async def _criar(cliente, h, nome, *, host, porta, segredo):
    return await cliente.post(
        "/conexoes",
        json={
            "nome": nome,
            "tipo": "sqlserver",
            "config": {"host": host, "porta": porta, "base": "master", "usuario": "sa"},
            "segredo": segredo,
        },
        headers=h,
    )


# --- always runs: proves the connector + sanitized error, no SQL Server needed ---
async def test_host_inacessivel_saneado(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="ss1@example.com", perfil="especialista"
    )
    cid = (
        await _criar(cliente, h, "ss-inacessivel", host="192.0.2.1", porta=1433, segredo="x")
    ).json()["id"]
    t = await cliente.post(f"/conexoes/{cid}/testar", headers=h)
    assert t.status_code == 200
    assert t.json()["ok"] is False
    assert t.json()["causa"]  # sanitized cause (no raw driver text)


# --- against a real SQL Server (mssql profile) ---
@pytest.mark.skipif(
    not _mssql_no_ar(), reason="SQL Server (docker-compose --profile mssql) unavailable"
)
class TestSqlServerReal:
    async def test_testar_ok(self, owner_engine, app_engine, cliente):
        _, h = await usuario_logado(
            owner_engine, cliente, email="ss2@example.com", perfil="especialista"
        )
        cid = (
            await _criar(
                cliente, h, "ss-ok", host=MSSQL_HOST, porta=MSSQL_PORT, segredo=MSSQL_SENHA
            )
        ).json()["id"]
        t = await cliente.post(f"/conexoes/{cid}/testar", headers=h)
        assert t.status_code == 200 and t.json()["ok"] is True

    async def test_navegar_schema(self, owner_engine, app_engine, cliente):
        _, h = await usuario_logado(
            owner_engine, cliente, email="ss3@example.com", perfil="especialista"
        )
        cid = (
            await _criar(
                cliente, h, "ss-nav", host=MSSQL_HOST, porta=MSSQL_PORT, segredo=MSSQL_SENHA
            )
        ).json()["id"]
        s = await cliente.get(f"/conexoes/{cid}/schema", headers=h)
        assert s.status_code == 200
        assert len(s.json()["schemas"]) >= 1  # master has system schemas/tables

    async def test_senha_errada_saneada(self, owner_engine, app_engine, cliente):
        _, h = await usuario_logado(
            owner_engine, cliente, email="ss4@example.com", perfil="especialista"
        )
        cid = (
            await _criar(
                cliente, h, "ss-pw", host=MSSQL_HOST, porta=MSSQL_PORT, segredo="wrong-password-1"
            )
        ).json()["id"]
        t = await cliente.post(f"/conexoes/{cid}/testar", headers=h)
        assert t.json()["ok"] is False
        assert t.json()["causa"]  # sanitized cause
        assert "wrong-password-1" not in t.text

    async def test_amostra_sqlserver(self, owner_engine, app_engine, cliente):
        # sample via TOP, which the preview uses over a SQL Server source
        from cflow_core.conexoes import service as cs

        _, h = await usuario_logado(
            owner_engine, cliente, email="ss5@example.com", perfil="especialista"
        )
        cid = (
            await _criar(
                cliente, h, "ss-am", host=MSSQL_HOST, porta=MSSQL_PORT, segredo=MSSQL_SENHA
            )
        ).json()["id"]
        async with app_engine.begin():
            cols, linhas = await cs.amostra(uuid.UUID(cid), "SELECT name FROM sys.databases", 5)
        assert "name" in cols
        assert len(linhas) >= 1 and "name" in linhas[0]
