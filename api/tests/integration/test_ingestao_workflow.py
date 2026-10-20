"""Ingestion through the ORCHESTRATOR: dedicated queue, one activity per slice.

The other tests exercise the engine inline (same functions); this one proves the production path -
the run is scheduled on the **ingestion** task queue (`cflow-ingestao-test`, isolated from the
pipelines queue) and the worker carries it to the end on its own.
"""

from __future__ import annotations

import asyncio
import io
import uuid
import zipfile
from datetime import date

import httpx
import pytest

from cflow_core.ingestao import service

from ._arnes_auth import usuario_logado
from .conftest import _WORKER
from .test_ingestao import CSV, contrato_registry

pytestmark = pytest.mark.integration


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("inf.csv", CSV.encode("latin-1"))
    return buf.getvalue()


async def _aguardar(cliente, execucao_id: str, headers: dict, timeout: float = 60.0) -> dict:
    if not _WORKER["ok"]:
        pytest.skip(f"Test Temporal worker unavailable ({_WORKER['motivo']}).")
    loop = asyncio.get_running_loop()
    fim = loop.time() + timeout
    ultimo: dict = {}
    while loop.time() < fim:
        ultimo = (await cliente.get(f"/ingestao/execucoes/{execucao_id}", headers=headers)).json()
        if ultimo.get("status") in ("concluida", "falha"):
            return ultimo
        await asyncio.sleep(0.25)
    raise AssertionError(f"ingestion {execucao_id} did not finish within {timeout}s")


async def test_ingestao_pelo_orquestrador_ate_o_bronze(
    cliente, owner_engine, monkeypatch, tmp_path
) -> None:
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: original(
            *a,
            **{
                **k,
                "transport": httpx.MockTransport(
                    lambda r: httpx.Response(200, content=_zip(), headers={"ETag": '"v1"'})
                ),
            },
        ),
    )

    _, headers = await usuario_logado(
        owner_engine, cliente, email="admin.wf@example.com", perfil="admin"
    )
    r = await cliente.post(
        "/ingestao/fontes",
        json={"nome": "Registry workflow", "contrato": contrato_registry("registry_wf")},
        headers=headers,
    )
    fid = r.json()["id"]

    disparo = await service.executar(
        fonte_id=uuid.UUID(fid),
        origem="manual",
        inicio=date(2026, 8, 1),
        fim=date(2026, 8, 31),
        ator_id=None,
    )
    # Asynchronous: the API returns 'executando' and the caller follows along via the run (no
    # "workflow" in the exposed vocabulary).
    assert disparo["status"] == "executando"

    detalhe = await _aguardar(cliente, disparo["execucao_id"], headers)
    assert detalhe["status"] == "concluida", detalhe.get("erro")
    assert detalhe["lotes_novos"] == 1
    assert detalhe["lotes"][0]["status"] == "bronze"
    assert list((tmp_path / "lake" / "bronze" / "registry_wf").rglob("*.parquet"))
    get_settings.cache_clear()
