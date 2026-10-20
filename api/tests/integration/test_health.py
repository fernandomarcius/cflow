"""Health check + the standard error envelope."""

from __future__ import annotations

import pytest

from cflow_core.request_context import CABECALHO_REQUEST_ID

pytestmark = pytest.mark.integration


async def test_saude_ok(cliente):
    resp = await cliente.get("/saude")
    assert resp.status_code == 200
    corpo = resp.json()
    assert corpo["status"] == "ok"
    assert corpo["banco"] == "ok"
    assert "momento" in corpo
    # `momento` converted to America/Sao_Paulo -> offset -03:00 (or -02:00 in summer time)
    assert corpo["momento"].endswith(("-03:00", "-02:00"))
    assert resp.headers.get(CABECALHO_REQUEST_ID)


async def test_saude_propaga_request_id(cliente):
    resp = await cliente.get("/saude", headers={CABECALHO_REQUEST_ID: "abc-123"})
    assert resp.headers.get(CABECALHO_REQUEST_ID) == "abc-123"


async def test_saude_banco_indisponivel_usa_envelope(cliente, monkeypatch):
    async def falha():
        raise RuntimeError("sem banco")

    monkeypatch.setattr("cflow_api.routes.health.verificar_conexao", falha)

    resp = await cliente.get("/saude")
    assert resp.status_code == 503
    corpo = resp.json()
    assert set(corpo["erro"]) >= {"codigo", "mensagem", "request_id"}
    assert corpo["erro"]["codigo"] == "servico_indisponivel"
    # human-readable message, with no secret/stack
    assert "unavailable" in corpo["erro"]["mensagem"].lower()
    assert corpo["erro"]["request_id"]


async def test_healthz_e_o_probe_de_orquestrador(cliente, monkeypatch):
    """`/healthz` is the conventional Kubernetes-style probe path: same body as `/saude` with the
    database up, and 503 without it, never 200 with a process unable to serve."""
    resp = await cliente.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["banco"] == "ok"

    async def falha():
        raise RuntimeError("sem banco")

    monkeypatch.setattr("cflow_api.routes.health.verificar_conexao", falha)
    resp = await cliente.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["erro"]["codigo"] == "servico_indisponivel"


async def test_prefixo_publico_api_v1_equivale_a_raiz(cliente):
    """The API also answers under the public prefix `/api/v1` (the `prefixo_publico` middleware):
    `/api/v1/saude` serves the same route as `/saude`."""
    raiz = await cliente.get("/saude")
    prefixada = await cliente.get("/api/v1/saude")
    assert raiz.status_code == prefixada.status_code == 200
    assert {k: v for k, v in prefixada.json().items() if k != "momento"} == {
        k: v for k, v in raiz.json().items() if k != "momento"
    }
    assert prefixada.headers.get(CABECALHO_REQUEST_ID)

    # the probe alias and the readiness route are reachable under the prefix too
    assert (await cliente.get("/api/v1/healthz")).status_code == 200
    # an unknown route stays unknown: the prefix does not turn everything into a hit
    assert (await cliente.get("/api/v1/rota-que-nao-existe")).status_code == 404
