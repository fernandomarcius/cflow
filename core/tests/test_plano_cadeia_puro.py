"""`disparo.resumir_cadeia`: what a trigger runs TOGETHER, without triggering it.

The scheduling screen consults this before the click. The decision on whether to publish is the same
as the trigger's (`decidir_montante`), and this proves the summary does not invent another one: with
the flag on the root and an upstream effect, the summary returns the SAME refusal the trigger would
give, as data.
"""

from __future__ import annotations

from cflow_core.execucao.disparo import resumir_cadeia


def _item(pid: str, nivel: int, nos: list[dict], versao: int = 3, **topo) -> dict:
    return {
        "pipeline_id": pid,
        "versao_numero": versao,
        "nivel": nivel,
        "definicao": {"nos": nos, "arestas": [], **topo},
    }


NOMES = {"u1": "orders_load", "u2": "crc", "r": "ref_05"}
SAIDA_GOLD = {
    "id": "saida",
    "tipo": "saida",
    "config": {"destino": "lake", "camada": "gold", "dataset": "orders"},
}
EMAIL = {"id": "avisa", "tipo": "email", "config": {"conexao_id": "c"}}


def test_pipeline_solo_nao_tem_montante():
    r = resumir_cadeia([_item("r", 0, [{"id": "n", "tipo": "sql"}])], "r", NOMES)
    assert r["montantes"] == [] and r["sem_publicar"] is False and r["recusa"] is None
    assert r["nome"] == "ref_05" and r["versao"] == 3


def test_montante_nomeia_efeitos_e_publicacoes():
    ordem = [
        _item("u1", 0, [SAIDA_GOLD, EMAIL], versao=7),
        _item("u2", 1, [{"id": "calc", "tipo": "python"}]),
        _item("r", 2, [{"id": "n", "tipo": "sql"}]),
    ]
    r = resumir_cadeia(ordem, "r", NOMES)
    assert [m["nome"] for m in r["montantes"]] == ["orders_load", "crc"]
    u1 = r["montantes"][0]
    assert u1["versao"] == 7 and u1["nivel"] == 0
    assert u1["efeitos"] == [{"no_id": "avisa", "tipo": "email"}]
    assert u1["publicacoes"] == [
        {
            "no_id": "saida",
            "destino": "lake",
            "alvo": "orders",
            "camada": "gold",
            "publica": True,
        }
    ]
    assert r["montantes"][1]["efeitos"] == [] and r["montantes"][1]["publicacoes"] == []


def test_raiz_marcada_sem_efeito_a_montante_nao_publica():
    ordem = [_item("u1", 0, [SAIDA_GOLD]), _item("r", 1, [], montante_publica=False)]
    r = resumir_cadeia(ordem, "r", NOMES)
    assert r["sem_publicar"] is True and r["recusa"] is None
    assert r["montantes"][0]["publicacoes"][0]["publica"] is False


def test_raiz_marcada_com_efeito_a_montante_devolve_a_recusa_do_disparo():
    ordem = [_item("u1", 0, [SAIDA_GOLD, EMAIL]), _item("r", 1, [], montante_publica=False)]
    r = resumir_cadeia(ordem, "r", NOMES)
    assert r["recusa"]["codigo"] == "montante_com_efeito"
    assert "orders_load" in r["recusa"]["mensagem"] and "avisa" in r["recusa"]["mensagem"]
    # Refused, the run does not happen: no "does not publish" promised for a trigger that fails.
    assert r["sem_publicar"] is False


def test_marca_numa_montante_nao_vale():
    # The flag concerns the REQUESTED run: on an upstream pipeline it turns nothing off.
    ordem = [_item("u1", 0, [SAIDA_GOLD], montante_publica=False), _item("r", 1, [])]
    r = resumir_cadeia(ordem, "r", NOMES)
    assert r["sem_publicar"] is False and r["montantes"][0]["publicacoes"][0]["publica"] is True
