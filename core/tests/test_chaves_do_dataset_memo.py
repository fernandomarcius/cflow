"""`_na_ordem_do_dataset` reads the index `chaves` ONCE per dataset (memo with a TTL), instead of
downloading the whole `_atual.json` - which grows one scope per load - for every `publicado`
source."""

from __future__ import annotations

import json

from cflow_core.ingestao import leitura


class LakeContando:
    def __init__(self, chaves):
        self.leituras = 0
        self._corpo = json.dumps({"chaves": chaves, "particoes": []}).encode()

    def raiz_uri(self, camada):
        return f"mem://{camada}/"

    async def ler(self, _camada, _caminho):
        self.leituras += 1
        return self._corpo


async def test_segunda_leitura_nao_baixa_o_indice(monkeypatch):
    monkeypatch.setattr(leitura, "_CHAVES_DO_DATASET", {})
    lake = LakeContando(["operacao_id", "data_registro"])
    pedido = {"data_registro": "2026-09-22", "operacao_id": "2"}
    for _ in range(3):
        r = await leitura._na_ordem_do_dataset(lake, "load/x", "gold", pedido, ambiente="dev")
        assert list(r) == ["operacao_id", "data_registro"]
    assert lake.leituras == 1


async def test_ttl_vencido_rele(monkeypatch):
    monkeypatch.setattr(leitura, "_CHAVES_DO_DATASET", {})
    monkeypatch.setattr(leitura, "_TTL_CHAVES_S", 0.0)
    lake = LakeContando(["operacao_id", "data_registro"])
    pedido = {"data_registro": "2026-09-22", "operacao_id": "2"}
    await leitura._na_ordem_do_dataset(lake, "load/y", "gold", pedido, ambiente="dev")
    await leitura._na_ordem_do_dataset(lake, "load/y", "gold", pedido, ambiente="dev")
    assert lake.leituras == 2
