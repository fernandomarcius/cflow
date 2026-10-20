"""Filtered query of the audit trail."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from ._arnes_auth import semear_usuario, usuario_logado

pytestmark = pytest.mark.integration


async def _seed(owner_engine, *, ator_id=None, acao="criar", recurso_tipo="usuario"):
    ator_tipo = "usuario" if ator_id else "sistema"
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO auditoria (ator_id, ator_tipo, acao, recurso_tipo, recurso_id) "
                "VALUES (:a, :at, :ac, :rt, :ri)"
            ),
            {"a": ator_id, "at": ator_tipo, "ac": acao, "rt": recurso_tipo, "ri": uuid.uuid4()},
        )


async def test_filtros(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="aud@example.com", perfil="auditor")
    u = await semear_usuario(owner_engine, email="alvo@example.com", perfil="operador")
    await _seed(owner_engine, ator_id=u, acao="criar", recurso_tipo="usuario")
    await _seed(owner_engine, ator_id=u, acao="editar", recurso_tipo="perfil")
    await _seed(
        owner_engine, acao="editar", recurso_tipo="cubo"
    )  # type that does not collide with the auditor's own login

    # by action
    r = await cliente.get("/auditoria?acao=criar", headers=h)
    assert r.status_code == 200 and len(r.json()) == 1
    # by actor (2 records for user u; the auditor's login has another actor)
    assert len((await cliente.get(f"/auditoria?ator_id={u}", headers=h)).json()) == 2
    # by resource (type exclusive to the seeds)
    assert len((await cliente.get("/auditoria?recurso_tipo=cubo", headers=h)).json()) == 1
    # empty
    assert (await cliente.get("/auditoria?acao=publicar", headers=h)).json() == []


async def test_paginacao(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="aud2@example.com", perfil="auditor")
    for _ in range(3):
        await _seed(owner_engine, acao="criar", recurso_tipo="conexao")
    p1 = (await cliente.get("/auditoria?recurso_tipo=conexao&pagina=1&tamanho=2", headers=h)).json()
    p2 = (await cliente.get("/auditoria?recurso_tipo=conexao&pagina=2&tamanho=2", headers=h)).json()
    assert len(p1) == 2 and len(p2) == 1
