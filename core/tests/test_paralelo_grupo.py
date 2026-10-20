"""Serial chain per group (`config.grupo_serial`) in `dependencias_dos_nos`.

What is proven: without a group nothing changes (and `grupos=False` reproduces the single queue even
WITH a group, which is the replay path); different groups do not wait for each other and the
internal order of each is kept; a serial node without a group fans in and becomes a barrier; and the
REAL topological order (`definicao.ordem_topologica`) interleaves the groups without making them
wait for each other."""

from __future__ import annotations

import asyncio

from cflow_core.execucao.paralelo import (
    declara_grupo_serial,
    dependencias_dos_nos,
    grupo_serial,
    rodar_por_dependencia,
)
from cflow_core.pipelines.definicao import ordem_topologica


def _def(nos, arestas):
    return {
        "nos": [
            {"id": i, "tipo": t, **({"config": {"grupo_serial": g}} if g else {})}
            for i, t, g in nos
        ],
        "arestas": [{"de": a, "para": b} for a, b in arestas],
    }


# The graph that motivated the group: a gate, two bases (b1/b2) with three queued effects each,
# and a group-less closing node that waits for both.
def _dois_grupos():
    return _def(
        [
            ("gate", "escrita_sql", None),
            ("h1", "http", "b1"),
            ("l1", "escrita_sql", "b1"),
            ("r1", "http", "b1"),
            ("h2", "http", "b2"),
            ("l2", "escrita_sql", "b2"),
            ("r2", "http", "b2"),
            ("x", "escrita_sql", None),
        ],
        [
            ("gate", "h1"),
            ("gate", "h2"),
            ("h1", "l1"),
            ("l1", "r1"),
            ("h2", "l2"),
            ("l2", "r2"),
            ("r1", "x"),
            ("r2", "x"),
        ],
    )


def test_sem_grupo_e_identico_ao_legado():
    d = _def(
        [
            ("f", "fonte", None),
            ("w1", "escrita_sql", None),
            ("w2", "escrita_sql", None),
            ("m", "email", None),
            ("s", "sql", None),
        ],
        [("f", "w1"), ("f", "w2"), ("f", "m"), ("f", "s")],
    )
    ordem = ["f", "w1", "s", "w2", "m"]
    esperado = {"f": [], "w1": ["f"], "s": ["f"], "w2": ["f", "w1"], "m": ["f", "w2"]}
    assert dependencias_dos_nos(d, ordem) == esperado
    assert dependencias_dos_nos(d, ordem, grupos=False) == esperado
    assert not declara_grupo_serial(d)


def test_grupos_false_reproduz_a_fila_unica_mesmo_com_grupo_declarado():
    # This is the replay path: a workflow started before the `grupo-serial` marker must redo the
    # old schedule, in which EVERY serial node waited for the previous one, group or not.
    d = _dois_grupos()
    ordem = ["gate", "h1", "h2", "l1", "l2", "r1", "r2", "x"]
    assert declara_grupo_serial(d)
    deps = dependencias_dos_nos(d, ordem, grupos=False)
    assert deps["h2"] == ["gate", "h1"] and deps["l1"] == ["h1", "h2"]
    assert deps["x"] == ["r1", "r2"], (
        "single queue: x waits only for the last serial (r2) and the r1 edge"
    )


def test_grupos_independentes_nao_se_esperam_e_ordem_interna_e_preservada():
    d = _dois_grupos()
    deps = dependencias_dos_nos(d, ["gate", "h1", "h2", "l1", "l2", "r1", "r2", "x"])
    assert deps["h1"] == ["gate"] and deps["h2"] == ["gate"], "both bases start from the same gate"
    assert "h1" not in deps["h2"] and "h2" not in deps["l1"] and "l2" not in deps["r1"], (
        "groups do not wait for each other"
    )
    assert deps["l1"] == ["h1"] and deps["r1"] == ["l1"], "internal group order preserved"
    assert deps["l2"] == ["h2"] and deps["r2"] == ["l2"]
    assert {"r1", "r2"} <= set(deps["x"]), (
        "fan-in: the group-less node waits for the end of ALL groups"
    )


def test_ordem_topologica_real_intercala_os_grupos_sem_os_fazer_esperar():
    # The order the engine uses is `ordem_topologica` (by level, queued): the b1 and b2 nodes come
    # out INTERLEAVED. The test above wrote that arrangement by hand; here it is derived.
    d = _dois_grupos()
    ordem = ordem_topologica(d)
    assert ordem == ["gate", "h1", "h2", "l1", "l2", "r1", "r2", "x"]
    deps = dependencias_dos_nos(d, ordem)
    b1, b2 = {"h1", "l1", "r1"}, {"h2", "l2", "r2"}
    for no in b1:
        assert not (set(deps[no]) & b2), f"{no} (b1) waits for a b2 node: {deps[no]}"
    for no in b2:
        assert not (set(deps[no]) & b1), f"{no} (b2) waits for a b1 node: {deps[no]}"


async def test_as_duas_bases_rodam_ao_mesmo_tempo_e_o_fecho_espera_as_duas():
    d = _dois_grupos()
    ordem = ordem_topologica(d)
    segura = {"h1": asyncio.Event(), "h2": asyncio.Event()}
    iniciados: list[str] = []
    em_voo = 0
    pico = 0

    async def rodar(item: str) -> None:
        nonlocal em_voo, pico
        iniciados.append(item)
        em_voo += 1
        pico = max(pico, em_voo)
        try:
            if item in segura:
                await segura[item].wait()
            else:
                await asyncio.sleep(0)
        finally:
            em_voo -= 1

    tarefa = asyncio.create_task(
        rodar_por_dependencia(ordem, dependencias_dos_nos(d, ordem), rodar)
    )
    for _ in range(10):
        await asyncio.sleep(0)
    assert iniciados == ["gate", "h1", "h2"], (
        "h2 did not wait for h1: both HTTP nodes are in flight"
    )
    assert pico == 2
    segura["h1"].set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert "r1" in iniciados and "l2" not in iniciados, "b1 advanced alone while h2 holds b2"
    segura["h2"].set()
    await tarefa
    assert iniciados[-1] == "x" and set(iniciados) == set(ordem)


def test_no_sem_grupo_no_meio_da_ordem_e_barreira_para_os_grupos():
    # The known trap: a group-less effect (notification e-mail, `arquivo` output) that falls in the
    # middle of the topological order closes both queues and execution goes back to serial.
    d = _def(
        [
            ("a", "http", "g1"),
            ("b", "http", "g2"),
            ("mid", "escrita_sql", None),
            ("c", "http", "g1"),
            ("d", "http", "g2"),
        ],
        [],
    )
    deps = dependencias_dos_nos(d, ["a", "b", "mid", "c", "d"])
    assert deps["a"] == [] and deps["b"] == []
    assert set(deps["mid"]) == {"a", "b"}
    assert deps["c"] == ["mid"] and deps["d"] == ["mid"]


def test_saida_fora_do_lake_tambem_entra_no_grupo_e_a_do_lake_nao():
    d = {
        "nos": [
            {"id": "f", "tipo": "fonte"},
            {"id": "h1", "tipo": "http", "config": {"grupo_serial": "b1"}},
            {"id": "arq", "tipo": "saida", "config": {"grupo_serial": "b1"}},
            {"id": "h2", "tipo": "http", "config": {"grupo_serial": "b2"}},
            {"id": "lk", "tipo": "saida", "config": {"destino": "lake", "dataset": "x"}},
        ],
        "arestas": [{"de": "f", "para": x} for x in ("h1", "arq", "h2", "lk")],
    }
    deps = dependencias_dos_nos(d, ["f", "h1", "arq", "h2", "lk"])
    assert deps["arq"] == ["f", "h1"], "`arquivo` output is serial and joins the b1 group queue"
    assert deps["h2"] == ["f"], "b2 does not wait for b1"
    assert deps["lk"] == ["f"], "`lake` output is not serial: outside every queue"


def test_grupo_le_de_config_e_so_texto_nao_vazio_conta():
    assert grupo_serial({"tipo": "http", "config": {"grupo_serial": "b1"}}) == "b1"
    assert grupo_serial({"tipo": "http", "config": {"grupo_serial": " b1 "}}) == "b1"
    assert grupo_serial({"tipo": "http", "config": {"grupo_serial": ""}}) is None
    assert grupo_serial({"tipo": "http", "config": {"grupo_serial": "   "}}) is None
    assert grupo_serial({"tipo": "http"}) is None
    # A list/number does not become a queue with an odd name: validation refuses it; here it is the single queue.
    assert grupo_serial({"tipo": "http", "config": {"grupo_serial": ["b1"]}}) is None
    assert grupo_serial({"tipo": "http", "config": {"grupo_serial": 1}}) is None


def test_ordem_parcial_com_grupo():
    d = _def(
        [("h1", "http", "b1"), ("l1", "escrita_sql", "b1"), ("h2", "http", "b2")], [("h1", "l1")]
    )
    assert dependencias_dos_nos(d, ["l1", "h2"]) == {"l1": [], "h2": []}
