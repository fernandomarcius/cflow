"""`permitir_vazio` on a published `saida` node: an empty result publishes an empty parquet.

The case that motivated it: a load for a unit with no rows (for example, no cancellations) refused to
publish the empty dataset, and a downstream pipeline that reads it through `publicado` never found
the partition. With `permitir_vazio`, the partition is published empty and the read finds it.
"""

from __future__ import annotations

import pytest

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao, contrato_ambiente_gold

pytestmark = pytest.mark.integration

NOME = "Load (empty result)"


def _definicao(*, permitir_vazio: bool = False) -> dict:
    config = {
        "destino": "lake",
        "camada": "gold",
        "dataset": "test_empty_load",
        "particao": [{"origem": "parametro", "nome": "operacao_id"}],
    }
    if permitir_vazio:
        config["permitir_vazio"] = True
    return contrato_ambiente_gold(
        {
            "parametros": [{"nome": "operacao_id", "tipo": "texto", "obrigatorio": True}],
            "nos": [
                {"id": "base", "tipo": "sql", "sql": "SELECT 1 AS n WHERE :operacao_id = '0'"},
                {"id": "saida", "tipo": "saida", "config": config},
            ],
            "arestas": [{"de": "base", "para": "saida"}],
        }
    )


async def _publicar(cliente, h, definicao: dict, nome: str) -> str:
    definicao = contrato_ambiente_gold(definicao)
    pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code in (200, 201), r.text
    r = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    assert r.status_code in (200, 201), r.text
    return pid


async def _executar(cliente, h, pid: str) -> dict:
    r = await cliente.post(
        f"/pipelines/{pid}/executar",
        json={"parametros": {"ambiente": "dev", "operacao_id": "188"}},
        headers=h,
    )
    return await aguardar_execucao(cliente, r.json()["id"], h)


async def test_saida_vazia_publica_parquet_vazio_com_permitir_vazio(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """Without the flag, zero rows is `saida_vazia`; with it, the unit publishes the EMPTY dataset in
    its own partition, so a read through `publicado` has something to find."""
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="empty3@example.com", perfil="admin"
        )
        recusa = await _publicar(cliente, h, _definicao(), f"{NOME} strict")
        det = await _executar(cliente, h, recusa)
        assert det["status"] == "falha", "without the flag, zero rows is saida_vazia"

        pid = await _publicar(cliente, h, _definicao(permitir_vazio=True), NOME)
        det = await _executar(cliente, h, pid)
        assert det["status"] == "concluida", det
        escopo = tmp_path / "lake" / "gold" / "dev" / "test_empty_load" / "operacao_id=188"
        assert (escopo / "_atual.json").is_file(), "the unit partition exists, empty"
        assert (escopo / f"execucao_id={det['id']}" / "dados.parquet").is_file()
    finally:
        get_settings.cache_clear()
