"""File source connector: CSV and Parquet; test + sample + columns."""

from __future__ import annotations

import tempfile
import uuid

import pytest

from cflow_core.conexoes import service as cs

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


async def _criar(cliente, h, nome, caminho):
    return await cliente.post(
        "/conexoes",
        json={"nome": nome, "tipo": "arquivo", "config": {"caminho": caminho}},
        headers=h,
    )


async def test_csv(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="arq1@example.com", perfil="especialista"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        f.write("account,valor\n1,100\n2,200\n")
        caminho = f.name
    cid = (await _criar(cliente, h, "csv1", caminho)).json()["id"]
    assert (await cliente.post(f"/conexoes/{cid}/testar", headers=h)).json()["ok"] is True
    async with app_engine.begin():
        cols, linhas = await cs.amostra(uuid.UUID(cid), "", 10)
    assert cols == ["account", "valor"]  # CSV header
    assert len(linhas) == 2 and linhas[0]["account"] == "1" and linhas[1]["valor"] == "200"


async def test_parquet(owner_engine, app_engine, cliente):
    import duckdb

    _, h = await usuario_logado(
        owner_engine, cliente, email="arq2@example.com", perfil="especialista"
    )
    caminho = tempfile.mktemp(suffix=".parquet")
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT 1 AS n, 'a' AS t UNION ALL SELECT 2, 'b') TO '{caminho}' (FORMAT parquet)"
    )
    con.close()
    cid = (await _criar(cliente, h, "pq1", caminho)).json()["id"]
    assert (await cliente.post(f"/conexoes/{cid}/testar", headers=h)).json()["ok"] is True
    async with app_engine.begin():
        cols, linhas = await cs.amostra(uuid.UUID(cid), "", 10)
    assert {"n", "t"} <= set(cols)  # parquet schema
    assert len(linhas) == 2 and {"n", "t"} <= set(linhas[0].keys())


async def test_arquivo_inexistente_saneado(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="arq3@example.com", perfil="especialista"
    )
    cid = (await _criar(cliente, h, "naoexiste", "/tmp/nao-existe-12345.csv")).json()["id"]
    t = await cliente.post(f"/conexoes/{cid}/testar", headers=h)
    assert t.json()["ok"] is False and t.json()["causa"] == "file not found"
