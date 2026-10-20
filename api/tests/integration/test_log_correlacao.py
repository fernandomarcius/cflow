"""Log correlation per request."""

from __future__ import annotations

import pytest

from cflow_core.request_context import CABECALHO_REQUEST_ID

pytestmark = pytest.mark.integration


async def test_request_id_correlaciona_a_requisicao(app_engine, cliente):
    # the whole request (logs included) shares the same request_id, reflected in the response
    r = await cliente.get("/saude", headers={CABECALHO_REQUEST_ID: "corr-123"})
    assert r.headers.get(CABECALHO_REQUEST_ID) == "corr-123"


async def test_request_id_gerado_quando_ausente(app_engine, cliente):
    r = await cliente.get("/saude")
    assert r.headers.get(CABECALHO_REQUEST_ID)
