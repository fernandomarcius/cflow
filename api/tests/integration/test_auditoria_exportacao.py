"""Audited CSV/Excel export."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


async def _seed(owner_engine, n=2):
    async with owner_engine.begin() as conn:
        for _ in range(n):
            await conn.execute(
                text(
                    "INSERT INTO auditoria (ator_tipo, acao, recurso_tipo, recurso_id) "
                    "VALUES ('sistema', 'criar', 'conexao', :ri)"
                ),
                {"ri": uuid.uuid4()},
            )


async def _conta_exportacoes(app_engine) -> int:
    async with app_engine.connect() as conn:
        return (
            await conn.execute(text("SELECT count(*) FROM auditoria WHERE acao='exportar'"))
        ).scalar_one()


async def test_csv_e_auditoria(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="aude@example.com", perfil="auditor")
    await _seed(owner_engine, 2)

    # a plain query is not audited
    antes = await _conta_exportacoes(app_engine)
    await cliente.get("/auditoria?recurso_tipo=conexao", headers=h)
    assert await _conta_exportacoes(app_engine) == antes

    r = await cliente.get("/auditoria/exportacao?formato=csv&recurso_tipo=conexao", headers=h)
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    linhas = r.text.strip().splitlines()
    assert linhas[0].startswith("ocorrido_em,")  # header
    assert len(linhas) == 1 + 2  # header + 2 records
    # the export generated a record in the trail
    assert await _conta_exportacoes(app_engine) == antes + 1


async def test_xlsx(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="audx@example.com", perfil="auditor")
    await _seed(owner_engine, 1)
    r = await cliente.get("/auditoria/exportacao?formato=xlsx", headers=h)
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    assert r.content[:2] == b"PK"  # xlsx is a zip


async def test_exportacao_vazia_so_cabecalho(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="audv@example.com", perfil="auditor")
    r = await cliente.get("/auditoria/exportacao?formato=csv&acao=publicar", headers=h)
    assert r.status_code == 200
    assert len(r.text.strip().splitlines()) == 1  # header only
