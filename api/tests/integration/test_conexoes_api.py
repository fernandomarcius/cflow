"""REST API source connector: test + sample + auth through the vault."""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cflow_core.conexoes import service as cs

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if (
            self.path.startswith("/protegido")
            and self.headers.get("Authorization") != "Bearer segredo-api"
        ):
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps([{"id": 1, "nome": "a"}, {"id": 2, "nome": "b"}]).encode())

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def servidor():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    porta = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield porta
    srv.shutdown()


async def _criar(cliente, h, nome, config, segredo=None):
    return await cliente.post(
        "/conexoes",
        json={"nome": nome, "tipo": "api_rest", "config": config, "segredo": segredo},
        headers=h,
    )


async def test_api_testar_e_amostra(owner_engine, app_engine, cliente, servidor):
    _, h = await usuario_logado(
        owner_engine, cliente, email="api1@example.com", perfil="especialista"
    )
    cid = (
        await _criar(
            cliente, h, "api-ok", {"url": f"http://127.0.0.1:{servidor}/dados", "metodo": "GET"}
        )
    ).json()["id"]
    assert (await cliente.post(f"/conexoes/{cid}/testar", headers=h)).json()["ok"] is True
    async with app_engine.begin():
        _cols, linhas = await cs.amostra(uuid.UUID(cid), "", 10)
    assert len(linhas) == 2 and linhas[0]["nome"] == "a"


async def test_api_auth_via_vault(owner_engine, app_engine, cliente, servidor):
    _, h = await usuario_logado(
        owner_engine, cliente, email="api2@example.com", perfil="especialista"
    )
    cfg = {"url": f"http://127.0.0.1:{servidor}/protegido", "metodo": "GET", "auth_tipo": "bearer"}
    # correct secret -> ok
    ok = (await _criar(cliente, h, "api-auth", cfg, segredo="segredo-api")).json()["id"]
    assert (await cliente.post(f"/conexoes/{ok}/testar", headers=h)).json()["ok"] is True
    # wrong secret -> refused, sanitised
    ruim = (await _criar(cliente, h, "api-ruim", cfg, segredo="errado")).json()["id"]
    t = await cliente.post(f"/conexoes/{ruim}/testar", headers=h)
    assert t.json()["ok"] is False and t.json()["causa"] == "authentication refused"


async def test_api_host_inacessivel(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="api3@example.com", perfil="especialista"
    )
    cid = (await _criar(cliente, h, "api-down", {"url": "http://127.0.0.1:1/x"})).json()["id"]
    t = await cliente.post(f"/conexoes/{cid}/testar", headers=h)
    assert t.json()["ok"] is False
