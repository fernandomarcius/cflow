"""`CFLOW_LAKE_SOMENTE_LOCAL` guard on the paths that PUBLISH (dev, CI and this harness).

The harness (`conftest._migrado`) turns the guard on for the whole suite, and that is what these
tests prove together: with the lake connection pointing at the `example-*` buckets - the repointing
that made a dev run publish to PRODUCTION -, the `lake` and `iceberg` outputs fail LOUDLY, with the
output node naming the guard, and the GCS backend is never even built (no credential read, no
request to the bucket). "Test connection" returns the same refusal as a verdict.
"""

from __future__ import annotations

import json

import pytest

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao, contrato_ambiente_gold, parametros_ambiente_gold

pytestmark = pytest.mark.integration

_BUCKETS_REMOTOS = {
    "landing": "example-landing",
    "bronze": "example-bronze",
    "quarantine": "example-quarantine",
    "silver": "example-silver",
    "gold": "example-gold",
}


@pytest.fixture
def gcs_proibido(monkeypatch):
    """If the guard fails, the test cannot depend on the network being down to turn red: building
    the GCS backend here is itself the failure. The test worker runs in this process, so it uses
    the same module."""
    from cflow_core.ingestao import armazenamento

    def _explode(*_a, **_kw):
        raise AssertionError("the GCS backend was built: the guard did not refuse first")

    monkeypatch.setattr(armazenamento, "ArmazenamentoGcs", _explode)


async def _conexao_do_bucket(cliente, h) -> str:
    r = await cliente.post(
        "/conexoes",
        json={
            "nome": "Data Lake (GCS)",
            "tipo": "objeto",
            "segredo": json.dumps({"type": "service_account", "private_key": "PEM"}),
            "config": {"provedor": "gcs", "buckets": _BUCKETS_REMOTOS},
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _rodar(cliente, h, saida: dict) -> dict:
    definicao = {
        "nos": [
            {"id": "src", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1),(2)) t(n)"},
            {"id": "out", "tipo": "saida", "config": saida},
        ],
        "arestas": [{"de": "src", "para": "out"}],
    }
    definicao = contrato_ambiente_gold(definicao)
    pid = (await cliente.post("/pipelines", json={"nome": "Guard"}, headers=h)).json()["id"]
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 200, r.text
    r = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    assert r.status_code == 201, r.text  # publishing creates the version
    ex = (
        await cliente.post(
            f"/pipelines/{pid}/executar",
            json={"parametros": parametros_ambiente_gold(definicao)},
            headers=h,
        )
    ).json()
    return await aguardar_execucao(cliente, ex["id"], h)


def _recusa_da_guarda(det: dict) -> None:
    assert det["status"] == "falha", det
    nos = {n["no_id"]: n for n in det["nos"]}
    # The OUTPUT is what fails, not the computation: the SQL node runs, and the refusal comes when opening the destination.
    assert nos["src"]["status"] != "falha", nos
    assert nos["out"]["status"] == "falha", nos
    # The cause must NAME the guard - "failed to write to the lake" would send people looking for a
    # credential. It lives on the NODE: with the workflow engine (the default) the run `erro` is the
    # generic node-failure message, and it is the node that the run screen opens in the error tab.
    assert "CFLOW_LAKE_SOMENTE_LOCAL" in (nos["out"]["erro"] or ""), nos["out"]


async def test_saida_lake_no_bucket_falha_alto_sem_montar_o_gcs(
    owner_engine, cliente, gcs_proibido
):
    _, h = await usuario_logado(owner_engine, cliente, email="slo1@example.com", perfil="admin")
    cx = await _conexao_do_bucket(cliente, h)
    det = await _rodar(
        cliente,
        h,
        {"destino": "lake", "camada": "gold", "dataset": "guard_local", "conexao_id": cx},
    )
    _recusa_da_guarda(det)


async def test_saida_iceberg_no_bucket_falha_alto_sem_montar_o_gcs(
    owner_engine, cliente, gcs_proibido
):
    _, h = await usuario_logado(owner_engine, cliente, email="slo2@example.com", perfil="admin")
    cx = await _conexao_do_bucket(cliente, h)
    det = await _rodar(
        cliente, h, {"destino": "iceberg", "dataset": "guard_iceberg", "conexao_id": cx}
    )
    _recusa_da_guarda(det)


async def test_testar_conexao_do_bucket_devolve_a_recusa(owner_engine, cliente, gcs_proibido):
    _, h = await usuario_logado(owner_engine, cliente, email="slo3@example.com", perfil="admin")
    cx = await _conexao_do_bucket(cliente, h)
    r = await cliente.post(f"/conexoes/{cx}/testar", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False
    assert "CFLOW_LAKE_SOMENTE_LOCAL" in (r.json().get("causa") or "")


async def test_saida_lake_local_continua_publicando(owner_engine, cliente, tmp_path, monkeypatch):
    """The guard refuses the BUCKET, not the lake: with no connection (the air-gapped default) the output publishes."""
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    try:
        _, h = await usuario_logado(owner_engine, cliente, email="slo4@example.com", perfil="admin")
        det = await _rodar(cliente, h, {"destino": "lake", "camada": "gold", "dataset": "ds_ok"})
        assert det["status"] == "concluida", det
        assert (tmp_path / "lake" / "gold" / "dev" / "ds_ok" / "_atual.json").is_file()
    finally:
        get_settings.cache_clear()
