"""Every write action produces exactly one complete record."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ._arnes_escrita import escrever_recurso_exemplo

pytestmark = pytest.mark.integration


async def test_escrita_gera_um_registro_completo(app_engine):
    recurso_id = await escrever_recurso_exemplo(app_engine, request_id="req-xyz")

    async with app_engine.connect() as conn:
        linhas = (
            (
                await conn.execute(
                    text(
                        "SELECT ator_id, ator_tipo, acao, recurso_tipo, recurso_id, "
                        "antes, depois, request_id, ocorrido_em FROM auditoria"
                    )
                )
            )
            .mappings()
            .all()
        )

    assert len(linhas) == 1
    r = linhas[0]
    assert r["ator_id"] is None
    assert r["ator_tipo"] == "sistema"
    assert r["acao"] == "criar"
    assert r["recurso_tipo"] == "recurso_teste"
    assert str(r["recurso_id"]) == str(recurso_id)
    assert r["antes"] is None  # creation -> no previous state
    assert set(r["depois"]) == {"nome"}
    assert r["request_id"] == "req-xyz"
    assert r["ocorrido_em"] is not None
