"""Every RBAC write is audited."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from cflow_core.auth import notificacao
from cflow_core.auth.notificacao import ProvedorMemoria

from ._arnes_auth import semear_usuario, usuario_logado

pytestmark = pytest.mark.integration


@pytest.fixture
def canal():
    mem = ProvedorMemoria()
    notificacao.definir_provedor(mem)
    yield mem
    notificacao.definir_provedor(None)


async def test_escritas_rbac_auditadas(owner_engine, app_engine, cliente, canal):
    _, h = await usuario_logado(owner_engine, cliente, email="aa@example.com", perfil="admin")
    alvo = await semear_usuario(owner_engine, email="t@example.com", perfil="operador")

    await cliente.post(
        "/usuarios",
        json={"email": "n@example.com", "nome": "N", "perfil": "operador"},
        headers=h,
    )
    await cliente.patch(f"/usuarios/{alvo}/perfil", json={"perfil": "especialista"}, headers=h)
    gid = (await cliente.post("/grupos", json={"nome": "GA"}, headers=h)).json()["id"]
    await cliente.post(
        "/permissoes",
        json={
            "sujeito_tipo": "grupo",
            "sujeito_id": gid,
            "recurso_tipo": "pagina",
            "recurso_id": str(uuid.uuid4()),
            "nivel": "ver",
        },
        headers=h,
    )

    async with app_engine.connect() as conn:
        recursos = (
            (await conn.execute(text("SELECT DISTINCT recurso_tipo FROM auditoria")))
            .scalars()
            .all()
        )
    assert {"usuario", "perfil", "grupo", "permissao"} <= set(recursos)
