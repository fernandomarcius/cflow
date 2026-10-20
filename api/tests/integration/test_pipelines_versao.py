"""US — versionamento: rascunho/publicar/reverter + lock otimista (SC-006)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

DEF = {"nos": [{"id": "a", "tipo": "sql", "sql": "SELECT 1"}], "arestas": []}


async def test_rascunho_publicar_reverter_e_lock(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="ev@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "pv"}, headers=h)).json()["id"]

    # rascunho base 0 → numero 1
    assert (
        await cliente.put(
            f"/pipelines/{pid}/rascunho", json={"definicao": DEF, "base_numero": 0}, headers=h
        )
    ).json()["numero"] == 1
    # rascunho base 1 → numero 2
    assert (
        await cliente.put(
            f"/pipelines/{pid}/rascunho", json={"definicao": DEF, "base_numero": 1}, headers=h
        )
    ).json()["numero"] == 2
    # lock otimista: base desatualizada → 409
    assert (
        await cliente.put(
            f"/pipelines/{pid}/rascunho", json={"definicao": DEF, "base_numero": 1}, headers=h
        )
    ).status_code == 409

    # publicar base 2 → numero 3
    pub = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 2, "nota": "v1"}, headers=h
    )
    assert pub.status_code == 201 and pub.json()["numero"] == 3

    # revert to v1
    rev = await cliente.post(f"/pipelines/{pid}/reverter", json={"para_numero": 1}, headers=h)
    assert rev.status_code == 201

    # auditoria registrou publicar e (criar pipeline)
    async with app_engine.connect() as conn:
        acoes = set(
            (await conn.execute(text("SELECT acao FROM auditoria WHERE recurso_tipo='pipeline'")))
            .scalars()
            .all()
        )
    assert {"criar", "publicar", "editar"} <= acoes  # criar + publicar + (rascunho/reverter=editar)
