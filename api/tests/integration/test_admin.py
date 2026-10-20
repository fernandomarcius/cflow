"""Admin panel: real metrics; admin only."""

from __future__ import annotations

import pytest

from ._arnes_auth import semear_usuario, usuario_logado

pytestmark = pytest.mark.integration


async def test_metricas_admin(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="adm@example.com", perfil="admin")
    await semear_usuario(owner_engine, email="op@example.com", perfil="operador")

    r = await cliente.get("/admin/metricas", headers=h)
    assert r.status_code == 200
    m = r.json()
    assert m["banco"] == "ok"
    assert m["usuarios"]["total"] >= 2
    assert m["usuarios"]["ativos"] >= 2
    assert "admin" in m["usuarios"]["por_perfil"] and "operador" in m["usuarios"]["por_perfil"]
    assert "auditoria" in m and "sessoes_ativas" in m


async def test_saude_conexoes(owner_engine, app_engine, cliente):
    import tempfile

    _, h = await usuario_logado(owner_engine, cliente, email="adms@example.com", perfil="admin")
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        f.write("a\n1\n")
        bom = f.name
    await cliente.post(
        "/conexoes", json={"nome": "ok", "tipo": "arquivo", "config": {"caminho": bom}}, headers=h
    )
    await cliente.post(
        "/conexoes",
        json={"nome": "ruim", "tipo": "arquivo", "config": {"caminho": "/tmp/nao-xyz.csv"}},
        headers=h,
    )

    r = await cliente.get("/admin/conexoes/saude", headers=h)
    assert r.status_code == 200
    s = {x["nome"]: x for x in r.json()}
    assert s["ok"]["ok"] is True
    assert s["ruim"]["ok"] is False and s["ruim"]["causa"]


async def test_nao_admin_negado(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="opa@example.com", perfil="operador")
    assert (await cliente.get("/admin/metricas", headers=h)).status_code == 403
