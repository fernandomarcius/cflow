"""Export/import of definitions between environments, with connection remapping."""

from __future__ import annotations

import tempfile

import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


async def test_export_import_remapeia_conexao(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="exp@example.com", perfil="admin")

    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        f.write("a\n1\n")
        caminho = f.name
    cid = (
        await cliente.post(
            "/conexoes",
            json={"nome": "src", "tipo": "arquivo", "config": {"caminho": caminho}},
            headers=h,
        )
    ).json()["id"]

    pid = (await cliente.post("/pipelines", json={"nome": "promo"}, headers=h)).json()["id"]
    definicao = {
        "nos": [{"id": "n1", "tipo": "fonte", "config": {"conexao_id": cid, "sql": "SELECT 1"}}],
        "arestas": [],
    }
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )

    bundle = (await cliente.get("/admin/exportar", headers=h)).json()
    assert any(c["nome"] == "src" for c in bundle["conexoes"])
    assert any(p["nome"] == "promo" for p in bundle["pipelines"])

    # simulate another environment: delete connections and pipelines
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE pipeline_versoes, pipelines, conexoes RESTART IDENTITY CASCADE")
        )

    res = (await cliente.post("/admin/importar", json={"bundle": bundle}, headers=h)).json()
    assert res["conexoes_criadas"] == 1 and res["pipelines_criados"] == 1

    # connection recreated with a NEW id; the pipeline points to the new id (remapped)
    cid2 = next(
        c["id"] for c in (await cliente.get("/conexoes", headers=h)).json() if c["nome"] == "src"
    )
    assert cid2 != cid
    novo_pid = next(
        p["id"] for p in (await cliente.get("/pipelines", headers=h)).json() if p["nome"] == "promo"
    )
    det = (await cliente.get(f"/pipelines/{novo_pid}", headers=h)).json()
    assert det["definicao"]["nos"][0]["config"]["conexao_id"] == cid2


async def test_importar_nao_sobrescreve(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="exp2@example.com", perfil="admin")
    await cliente.post(
        "/conexoes",
        json={"nome": "dup", "tipo": "arquivo", "config": {"caminho": "/tmp/x.csv"}},
        headers=h,
    )
    bundle = {
        "versao": 1,
        "conexoes": [
            {"id": "old-1", "nome": "dup", "tipo": "arquivo", "config": {"caminho": "/y.csv"}}
        ],
        "pipelines": [],
    }
    res = (await cliente.post("/admin/importar", json={"bundle": bundle}, headers=h)).json()
    assert res["conexoes_criadas"] == 0  # name already exists -> preserved
