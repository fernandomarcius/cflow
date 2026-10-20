"""No secret leaks into an API response or metadata.

Exercises the flows and looks for the known secrets (database passwords) in the responses and in
the metadata tables. The secrets come from the environment; the test reads the same values.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from ._arnes_escrita import escrever_recurso_exemplo

pytestmark = pytest.mark.integration


def _segredos_conhecidos() -> list[str]:
    seg = []
    for var in ("CFLOW_APP_PASSWORD", "POSTGRES_PASSWORD"):
        v = os.environ.get(var)
        if v:
            seg.append(v)
    # also extract the password embedded in the runtime URL, if any
    url = os.environ.get("CFLOW_DATABASE_URL", "")
    if "@" in url and ":" in url.split("@")[0]:
        seg.append(url.split("@")[0].rsplit(":", 1)[-1])
    return [s for s in set(seg) if s]


async def test_resposta_de_saude_nao_contem_segredo(cliente):
    segredos = _segredos_conhecidos()
    if not segredos:
        pytest.skip("No secret in the environment to check.")
    corpo = (await cliente.get("/saude")).text
    for s in segredos:
        assert s not in corpo


async def test_metadados_nao_contem_segredo(app_engine):
    segredos = _segredos_conhecidos()
    if not segredos:
        pytest.skip("No secret in the environment to check.")
    await escrever_recurso_exemplo(app_engine)
    async with app_engine.connect() as conn:
        dump = (
            await conn.execute(text("SELECT string_agg(auditoria::text, ' ') FROM auditoria"))
        ).scalar_one() or ""
    for s in segredos:
        assert s not in dump
