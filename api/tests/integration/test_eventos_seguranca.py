"""Security events are audited.

Fires simulated login, login failure and permission change events and confirms one record per
event, with the right fields.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from cflow_core.audit import (
    registrar_login,
    registrar_login_falha,
    registrar_permissao,
)

pytestmark = pytest.mark.integration


async def _semear_usuario(owner_engine) -> uuid.UUID:
    """Create a user (through the owner role) for the successful login to reference by FK."""
    async with owner_engine.begin() as conn:
        res = await conn.execute(
            text(
                "INSERT INTO usuarios (email, nome, perfil) "
                "VALUES (:e, 'Teste', 'operador') RETURNING id"
            ),
            {"e": f"teste-{uuid.uuid4()}@example.com"},
        )
        return res.scalar_one()


async def test_login_sucesso_gera_registro(owner_engine, app_engine):
    ator = await _semear_usuario(owner_engine)
    async with app_engine.begin() as conn:
        await registrar_login(conn, ator_id=ator, request_id="req-login")

    async with app_engine.connect() as conn:
        r = (
            (
                await conn.execute(
                    text("SELECT ator_id, ator_tipo, acao FROM auditoria WHERE acao = 'login'")
                )
            )
            .mappings()
            .all()
        )
    assert len(r) == 1
    assert str(r[0]["ator_id"]) == str(ator)
    assert r[0]["ator_tipo"] == "usuario"


async def test_login_falha_gera_registro_anonimo(app_engine):
    async with app_engine.begin() as conn:
        await registrar_login_falha(conn, email_tentado="errado@x.com")

    async with app_engine.connect() as conn:
        r = (
            (
                await conn.execute(
                    text(
                        "SELECT ator_id, ator_tipo, depois FROM auditoria "
                        "WHERE acao = 'login_falha'"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(r) == 1
    assert r[0]["ator_id"] is None
    assert r[0]["ator_tipo"] == "anonimo"
    assert r[0]["depois"] == {"email_tentado": "errado@x.com"}


async def test_alteracao_permissao_gera_registro_com_antes_depois(app_engine):
    pid = uuid.uuid4()
    async with app_engine.begin() as conn:
        await registrar_permissao(
            conn,
            permissao_id=pid,
            antes={"nivel": "ver"},
            depois={"nivel": "editar"},
        )

    async with app_engine.connect() as conn:
        r = (
            (
                await conn.execute(
                    text(
                        "SELECT recurso_id, antes, depois, ator_tipo FROM auditoria "
                        "WHERE acao = 'permissao'"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(r) == 1
    assert str(r[0]["recurso_id"]) == str(pid)
    assert r[0]["antes"] == {"nivel": "ver"}
    assert r[0]["depois"] == {"nivel": "editar"}
    assert r[0]["ator_tipo"] == "sistema"  # no authenticated actor here
