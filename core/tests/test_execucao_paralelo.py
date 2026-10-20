"""Dependency-driven scheduling (`execucao.paralelo`), which replaces the per-level barrier in
`ExecutarDAG` and the one-activity-at-a-time loop in `ExecutarPipeline`.

What is proven here is the SEMANTICS the switch must not change (nobody starts before its
dependencies; after a failure nothing new starts; what is in flight finishes; the first failure
propagates) and the GAIN that justifies it (whoever does not depend on the slow item does not
wait for it). No Temporal or database: the same function runs inside the workflow and inline."""

from __future__ import annotations

import asyncio

import pytest

from cflow_core.execucao.paralelo import (
    TIPOS_SERIAIS,
    dependencias_dos_nos,
    rodar_por_dependencia,
)


class Registro:
    """Test `rodar`: records start/end and lets the test hold an item until an event."""

    def __init__(
        self, segurar: dict[str, asyncio.Event] | None = None, falhar: set[str] | None = None
    ):
        self.eventos: list[tuple[str, str]] = []
        self.segurar = segurar or {}
        self.falhar = falhar or set()
        self.em_voo = 0
        self.pico = 0

    async def __call__(self, item: str) -> None:
        self.eventos.append(("inicio", item))
        self.em_voo += 1
        self.pico = max(self.pico, self.em_voo)
        try:
            if item in self.segurar:
                await self.segurar[item].wait()
            else:
                await asyncio.sleep(0)
            if item in self.falhar:
                raise RuntimeError(f"failed {item}")
        finally:
            self.em_voo -= 1
            self.eventos.append(("fim", item))

    def iniciados(self) -> list[str]:
        return [i for tipo, i in self.eventos if tipo == "inicio"]

    def indice(self, tipo: str, item: str) -> int:
        return self.eventos.index((tipo, item))


async def test_quem_nao_depende_do_lento_nao_espera_por_ele():
    # `fast` is quick, `slow` is slow, and `mid` depends ONLY on `fast`. Per level it would wait
    # for `slow`; per dependency it starts as soon as `fast` finishes.
    lenta = asyncio.Event()
    reg = Registro(segurar={"slow": lenta})
    ordem = ["fast", "slow", "mid", "root"]
    deps = {"mid": ["fast"], "root": ["mid", "slow"]}

    tarefa = asyncio.create_task(rodar_por_dependencia(ordem, deps, reg))
    for _ in range(20):
        await asyncio.sleep(0)
    assert ("fim", "mid") in reg.eventos, "mid waited for slow, which it does not read"
    assert ("inicio", "root") not in reg.eventos, "root started before slow finished"
    lenta.set()
    await tarefa
    assert reg.indice("inicio", "root") > reg.indice("fim", "slow")


async def test_ninguem_comeca_antes_de_todas_as_dependencias():
    reg = Registro()
    ordem = ["a", "b", "c", "d"]
    deps = {"c": ["a", "b"], "d": ["c"]}
    await rodar_por_dependencia(ordem, deps, reg)
    for item, ds in deps.items():
        for dep in ds:
            assert reg.indice("inicio", item) > reg.indice("fim", dep)


async def test_sem_dependencia_todos_em_paralelo_e_cadeia_linear_em_serie():
    evento = asyncio.Event()
    reg = Registro(segurar={x: evento for x in "abc"})
    tarefa = asyncio.create_task(rodar_por_dependencia(["a", "b", "c"], {}, reg))
    for _ in range(10):
        await asyncio.sleep(0)
    assert reg.pico == 3
    evento.set()
    await tarefa

    reg = Registro()
    await rodar_por_dependencia(["a", "b", "c"], {"b": ["a"], "c": ["b"]}, reg)
    assert reg.pico == 1
    assert reg.iniciados() == ["a", "b", "c"]


async def test_falha_nada_novo_comeca_o_em_voo_termina_e_a_falha_sobe():
    # `a` fails while `b` (independent) is still running. `filho_a` never starts (its dependency
    # failed); neither does `filho_b`: its dependency finished fine, but AFTER the failure, and
    # after the first failure nothing new starts. `b` finishes: abandoned, it would stay 'running'.
    segura_b = asyncio.Event()
    reg = Registro(segurar={"b": segura_b}, falhar={"a"})
    ordem = ["a", "b", "filho_a", "filho_b"]
    deps = {"filho_a": ["a"], "filho_b": ["b"]}
    tarefa = asyncio.create_task(rodar_por_dependencia(ordem, deps, reg))
    for _ in range(10):
        await asyncio.sleep(0)
    assert ("fim", "a") in reg.eventos and ("fim", "b") not in reg.eventos
    segura_b.set()
    with pytest.raises(RuntimeError, match="failed a"):
        await tarefa
    assert ("fim", "b") in reg.eventos
    assert "filho_a" not in reg.iniciados()
    assert "filho_b" not in reg.iniciados()


async def test_duas_falhas_sobe_a_primeira_da_lista():
    reg = Registro(falhar={"x", "y"})
    with pytest.raises(RuntimeError, match="failed x"):
        await rodar_por_dependencia(["x", "y"], {}, reg)


async def test_dependencia_fora_da_lista_ja_conta_como_satisfeita():
    # `desde_no` re-execution: the upstream entered as completed and is not in the order.
    reg = Registro()
    await rodar_por_dependencia(["b", "c"], {"b": ["a"], "c": ["b"]}, reg)
    assert reg.iniciados() == ["b", "c"]


def test_dependencia_depois_do_dependente_e_recusada_antes_de_rodar():
    # An inverted edge would hang the run waiting on a future that never resolves.
    async def nunca(_):  # pragma: no cover - must not be called
        raise AssertionError("ran an item with an invalid order")

    with pytest.raises(ValueError, match="does not come before"):
        asyncio.run(rodar_por_dependencia(["b", "a"], {"b": ["a"]}, nunca))
    with pytest.raises(ValueError, match="Repeated"):
        asyncio.run(rodar_por_dependencia(["a", "a"], {}, nunca))


async def test_ordem_de_inicio_e_deterministica():
    # In Temporal, replay must schedule the SAME activities in the SAME order.
    ordem = ["f1", "f2", "f3", "j1", "j2", "raiz"]
    deps = {"j1": ["f1", "f2"], "j2": ["f2", "f3"], "raiz": ["j1", "j2"]}
    vistas = []
    for _ in range(5):
        reg = Registro()
        await rodar_por_dependencia(ordem, deps, reg)
        vistas.append(reg.iniciados())
    assert all(v == vistas[0] for v in vistas)
    assert vistas[0] == ordem


# ------------------------------------------------------------------------------------------------
# dependencies of the nodes of ONE pipeline
# ------------------------------------------------------------------------------------------------


def _def(nos: list[tuple[str, str]], arestas: list[tuple[str, str]]) -> dict:
    # `saida` here is the lake one: an output outside the lake has its own test, in the queue.
    def _no(i: str, t: str) -> dict:
        if t == "saida":
            return {"id": i, "tipo": t, "config": {"destino": "lake", "dataset": i}}
        return {"id": i, "tipo": t}

    return {
        "nos": [_no(i, t) for i, t in nos],
        "arestas": [{"de": de, "para": para} for de, para in arestas],
    }


def test_nos_dependem_das_arestas_e_fontes_nao_dependem_de_nada():
    d = _def(
        [("f1", "fonte"), ("f2", "fonte"), ("j", "sql"), ("g1", "saida"), ("g2", "saida")],
        [("f1", "j"), ("f2", "j"), ("j", "g1"), ("j", "g2")],
    )
    deps = dependencias_dos_nos(d, ["f1", "f2", "j", "g1", "g2"])
    assert deps == {"f1": [], "f2": [], "j": ["f1", "f2"], "g1": ["j"], "g2": ["j"]}


def test_nos_de_efeito_continuam_um_por_vez_na_ordem_de_sempre():
    # Two `escrita_sql` nodes with no edge between them: the topological order between them was an
    # implicit contract (write A before B), and an external database cannot undo it. They stay queued.
    d = _def(
        [
            ("f", "fonte"),
            ("w1", "escrita_sql"),
            ("w2", "escrita_sql"),
            ("m", "email"),
            ("s", "sql"),
        ],
        [("f", "w1"), ("f", "w2"), ("f", "m"), ("f", "s")],
    )
    deps = dependencias_dos_nos(d, ["f", "w1", "s", "w2", "m"])
    assert deps["w1"] == ["f"]
    assert deps["w2"] == ["f", "w1"]
    assert deps["m"] == ["f", "w2"]
    assert deps["s"] == ["f"], "a node without side effects does not join the effects queue"
    assert {"escrita_sql", "http", "email", "ingestao"} <= TIPOS_SERIAIS


def test_ordem_parcial_ignora_o_montante_que_ficou_de_fora():
    d = _def([("f", "fonte"), ("j", "sql"), ("s", "saida")], [("f", "j"), ("j", "s")])
    assert dependencias_dos_nos(d, ["j", "s"]) == {"j": [], "s": ["j"]}


async def test_pipeline_real_com_dependencias_dos_nos_respeita_as_arestas():
    d = _def(
        [("f1", "fonte"), ("f2", "fonte"), ("j", "sql"), ("g1", "saida"), ("g2", "saida")],
        [("f1", "j"), ("f2", "j"), ("j", "g1"), ("j", "g2")],
    )
    ordem = ["f1", "f2", "j", "g1", "g2"]
    reg = Registro()
    await rodar_por_dependencia(ordem, dependencias_dos_nos(d, ordem), reg)
    assert reg.indice("inicio", "j") > max(reg.indice("fim", "f1"), reg.indice("fim", "f2"))
    assert reg.indice("inicio", "g2") < reg.indice("fim", "g1"), "the leaves did not run together"


async def test_limite_por_execucao_segura_a_onda_larga():
    # The activity budget belongs to the WORKER. A wave of 12 leaves must not take every slot, or
    # the sibling chains would be left queued.
    evento = asyncio.Event()
    reg = Registro(segurar={f"g{i}": evento for i in range(12)})
    ordem = [f"g{i}" for i in range(12)]
    tarefa = asyncio.create_task(rodar_por_dependencia(ordem, {}, reg, limite=4))
    for _ in range(20):
        await asyncio.sleep(0)
    assert reg.pico == 4 and reg.iniciados() == ordem[:4], "the slot was not granted in list order"
    evento.set()
    await tarefa
    assert sorted(reg.iniciados()) == sorted(ordem)
    assert reg.pico == 4


async def test_falha_enquanto_esperava_vaga_o_proximo_nao_comeca():
    segura = asyncio.Event()
    reg = Registro(segurar={"a": segura}, falhar={"a"})
    tarefa = asyncio.create_task(rodar_por_dependencia(["a", "b"], {}, reg, limite=1))
    for _ in range(5):
        await asyncio.sleep(0)
    segura.set()
    with pytest.raises(RuntimeError, match="failed a"):
        await tarefa
    assert reg.iniciados() == ["a"], "b got the slot after the failure"


def test_saida_fora_do_lake_entra_na_fila_e_a_do_lake_nao():
    d = {
        "nos": [
            {"id": "f", "tipo": "fonte"},
            {"id": "g1", "tipo": "saida", "config": {"destino": "lake", "dataset": "a"}},
            {"id": "g2", "tipo": "saida", "config": {"destino": "lake", "dataset": "b"}},
            {"id": "arq1", "tipo": "saida", "config": {}},  # default destination = file
            {"id": "ice", "tipo": "saida", "config": {"destino": "iceberg"}},
        ],
        "arestas": [{"de": "f", "para": x} for x in ("g1", "g2", "arq1", "ice")],
    }
    deps = dependencias_dos_nos(d, ["f", "g1", "arq1", "g2", "ice"])
    assert deps["g1"] == ["f"] and deps["g2"] == ["f"]
    assert deps["arq1"] == ["f"] and deps["ice"] == ["f", "arq1"]
