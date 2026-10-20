"""Segregation of duties: what the specialist does NOT manage.

The specialist's reach was widened later: it creates and manages users with the `operador`
profile (see `test_usuarios_especialista.py`). This test guards what did NOT move: groups, profile
changes, fine-grained permissions, and creating a user of any other profile. It is the half that
keeps the widening from becoming an escalation, which is why the "create operator" assertion left
this file instead of the whole file leaving.
"""

from __future__ import annotations

import uuid

import pytest

from ._arnes_auth import semear_usuario, usuario_logado

pytestmark = pytest.mark.integration


async def test_especialista_nao_gere_identidade(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="espx@example.com", perfil="especialista"
    )
    alvo = await semear_usuario(owner_engine, email="tgt@example.com", perfil="operador")

    # create a user with a profile other than operator (creating an OPERATOR is theirs, and only that)
    for perfil in ("admin", "especialista", "auditor"):
        assert (
            await cliente.post(
                "/usuarios",
                json={"email": f"x-{perfil}@example.com", "nome": "X", "perfil": perfil},
                headers=h,
            )
        ).status_code == 403
    # create a group
    assert (await cliente.post("/grupos", json={"nome": "G"}, headers=h)).status_code == 403
    # change a profile, even of an OPERATOR whom they manage in everything else: this route is
    # what separates "creates an operator" from "creates an admin in two steps"
    assert (
        await cliente.patch(f"/usuarios/{alvo}/perfil", json={"perfil": "admin"}, headers=h)
    ).status_code == 403
    assert (
        await cliente.patch(f"/usuarios/{alvo}/perfil", json={"perfil": "auditor"}, headers=h)
    ).status_code == 403
    # grant a permission
    assert (
        await cliente.post(
            "/permissoes",
            json={
                "sujeito_tipo": "usuario",
                "sujeito_id": str(alvo),
                "recurso_tipo": "pagina",
                "recurso_id": str(uuid.uuid4()),
                "nivel": "ver",
            },
            headers=h,
        )
    ).status_code == 403
