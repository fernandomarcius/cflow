"""Lake publication in LEVELS: what runs together and what stays in order.

On GCS each round trip costs ~200-330 ms and partitioned gold publishing did 6 in series. Now there
are 3 levels - data || manifest; pointers || immutable index || read of `_atual.json`; write of
`_atual.json` - and what is a contract stays: the dataset mutable object only changes after the
data, the manifest, the scope pointers and the immutable index exist, and a failure in any of them
leaves `_atual.json` as it was.

Also: the own cap of the nodes that only publish (`paralelo.MAX_NOS_IO_EM_PARALELO`).

No database: the advisory lock and the `SELECT pipeline_id` become doubles; the lake is the local
backend under `tmp`, wrapped by one that records the order of writes and can fail on a chosen path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid

import duckdb
import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.execucao import engine
from cflow_core.execucao.paralelo import rodar_por_dependencia, so_publica
from cflow_core.ingestao import armazenamento, leitura

EID = uuid.UUID("33333333-3333-4333-8333-333333333333")
PID = uuid.UUID("44444444-4444-4444-8444-444444444444")
PARAMETROS = {"operacao_id": "2", "competencia": "2026-08", "ambiente": "dev"}
TIPOS = {"operacao_id": "texto", "competencia": "texto", "ambiente": "opcoes"}
PARTICAO = [
    {"origem": "parametro", "nome": "operacao_id"},
    {"origem": "parametro", "nome": "competencia"},
]


class LakeRegistrado:
    """Local backend that records each write (in order of COMPLETION) and fails when the path
    contains `falhar_em`."""

    def __init__(self, base, falhar_em: str | None = None):
        self.base = base
        self.falhar_em = falhar_em
        self.gravados: list[str] = []

    def __getattr__(self, nome):
        return getattr(self.base, nome)

    async def gravar(self, camada, caminho, dados, **kw):
        await asyncio.sleep(0)
        if self.falhar_em and self.falhar_em in caminho:
            raise ErroAplicacao(codigo="falha_gravacao_lake", mensagem="simulated", status=502)
        r = await self.base.gravar(camada, caminho, dados, **kw)
        self.gravados.append(caminho)
        return r

    async def gravar_arquivo(self, camada, caminho, origem, **kw):
        await asyncio.sleep(0)
        if self.falhar_em and self.falhar_em in caminho:
            raise ErroAplicacao(codigo="falha_gravacao_lake", mensagem="simulated", status=502)
        r = await self.base.gravar_arquivo(camada, caminho, origem, **kw)
        self.gravados.append(caminho)
        return r


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    monkeypatch.setenv("CFLOW_SNAPSHOT_DIR", str(tmp_path / "snap"))
    get_settings.cache_clear()

    @contextlib.asynccontextmanager
    async def _sem_trava(_chave):
        yield

    async def _pipeline(_e):
        return PID

    monkeypatch.setattr(leitura, "trava_consultiva", _sem_trava)
    monkeypatch.setattr(engine, "_pipeline_da_execucao", _pipeline)
    (tmp_path / "snap" / str(EID)).mkdir(parents=True)
    yield tmp_path
    get_settings.cache_clear()


def _local(tmp):
    return armazenamento.ArmazenamentoLocal(raiz=tmp / "lake")


def _usar_lake(monkeypatch, lake):
    async def _abrir(_conexao_id=None):
        return lake

    monkeypatch.setattr(armazenamento, "abrir", _abrir)


def _duck_com_snapshot(no_id: str, linhas: int = 3):
    duck = duckdb.connect()
    duck.execute(f'CREATE TABLE "{no_id}" AS SELECT range AS id FROM range({linhas})')
    duck.execute(
        f"COPY \"{no_id}\" TO '{engine._dir(EID) / (no_id + '.parquet')}' (FORMAT parquet)"
    )
    return duck


def _no(no_id: str, dataset: str) -> dict:
    return {
        "id": no_id,
        "tipo": "saida",
        "config": {"destino": "lake", "camada": "gold", "dataset": dataset, "particao": PARTICAO},
    }


async def _publicar(no_id: str, dataset: str, **kw):
    duck = _duck_com_snapshot(no_id)
    try:
        return await engine._publicar_saida(
            duck,
            _no(no_id, dataset),
            no_id,
            3,
            execucao_id=EID,
            parametros=PARAMETROS,
            tipos_parametro=TIPOS,
            **kw,
        )
    finally:
        duck.close()


async def test_mutavel_do_dataset_so_muda_depois_de_todo_o_resto(ambiente, monkeypatch):
    lake = LakeRegistrado(_local(ambiente))
    _usar_lake(monkeypatch, lake)
    await _publicar("gold_a", "ref/teste/a")

    atual = "dev/ref/teste/a/_atual.json"
    assert lake.gravados[-1] == atual, lake.gravados
    antes_dele = set(lake.gravados[:-1])
    assert any(c.endswith("/dados.parquet") for c in antes_dele)
    assert any(c.endswith("/_manifesto.json") for c in antes_dele)
    assert any(c.endswith("/_indice.json") for c in antes_dele)
    assert any(c.endswith("competencia=2026-08/_atual.json") for c in antes_dele)
    indice = json.loads((ambiente / "lake" / "gold" / atual).read_text())
    assert [p["linhas"] for p in indice["particoes"]] == [3]


@pytest.mark.parametrize("falha", ["_indice.json", "competencia=2026-08/_atual.json"])
async def test_falha_antes_do_mutavel_deixa_o_atual_como_estava(ambiente, monkeypatch, falha):
    # A good publication first: it is its `_atual.json` that must survive.
    bom = LakeRegistrado(_local(ambiente))
    _usar_lake(monkeypatch, bom)
    await _publicar("gold_b", "ref/teste/b")
    atual = ambiente / "lake" / "gold" / "dev/ref/teste/b/_atual.json"
    antes = atual.read_bytes()

    ruim = LakeRegistrado(_local(ambiente), falhar_em=falha)
    _usar_lake(monkeypatch, ruim)
    monkeypatch.setattr(engine, "_dir", lambda _e: ambiente / "snap" / str(EID))
    outro = uuid.UUID("55555555-5555-4555-8555-555555555555")
    duck = _duck_com_snapshot("gold_b")
    try:
        with pytest.raises(ErroAplicacao):
            await engine._publicar_saida(
                duck,
                _no("gold_b", "ref/teste/b"),
                "gold_b",
                3,
                execucao_id=outro,
                parametros=PARAMETROS,
                tipos_parametro=TIPOS,
            )
    finally:
        duck.close()
    assert atual.read_bytes() == antes
    assert "dev/ref/teste/b/_atual.json" not in ruim.gravados


async def test_falha_no_dado_espera_o_manifesto_e_nao_move_ponteiro(ambiente, monkeypatch):
    lake = LakeRegistrado(_local(ambiente), falhar_em="dados.parquet")
    _usar_lake(monkeypatch, lake)
    with pytest.raises(ErroAplicacao):
        await _publicar("gold_c", "ref/teste/c")
    # The manifest ran alongside and finished before the failure surfaced; no pointer was moved.
    assert any(c.endswith("_manifesto.json") for c in lake.gravados)
    assert not any(c.endswith("_atual.json") for c in lake.gravados)


async def test_saida_devolve_a_vaga_duckdb_antes_da_rede(ambiente, monkeypatch):
    ordem: list[str] = []

    class LakeQueAnota(LakeRegistrado):
        async def gravar_arquivo(self, camada, caminho, origem, **kw):
            ordem.append("rede")
            return await super().gravar_arquivo(camada, caminho, origem, **kw)

    _usar_lake(monkeypatch, LakeQueAnota(_local(ambiente)))

    async def _liberar():
        ordem.append("liberou")

    await _publicar("gold_d", "ref/teste/d", liberar_duck=_liberar)
    assert ordem[:2] == ["liberou", "rede"]


def test_so_publica_reconhece_apenas_a_saida_lake():
    assert so_publica({"tipo": "saida", "config": {"destino": "lake"}})
    assert not so_publica({"tipo": "saida", "config": {"destino": "arquivo"}})
    assert not so_publica({"tipo": "saida", "config": {}})
    assert not so_publica({"tipo": "sql"})


async def test_teto_de_io_e_separado_do_teto_de_cpu():
    em_voo = {"cpu": 0, "io": 0}
    pico = {"cpu": 0, "io": 0}
    classe = {**{f"c{i}": "cpu" for i in range(6)}, **{f"g{i}": "io" for i in range(10)}}

    async def rodar(item):
        k = classe[item]
        em_voo[k] += 1
        pico[k] = max(pico[k], em_voo[k])
        await asyncio.sleep(0.01)
        em_voo[k] -= 1

    ordem = list(classe)
    await rodar_por_dependencia(
        ordem,
        {i: [] for i in ordem},
        rodar,
        limite=2,
        io=lambda i: classe[i] == "io",
        limite_io=5,
    )
    assert pico == {"cpu": 2, "io": 5}


async def test_sem_teto_de_io_tudo_divide_o_mesmo_teto():
    em_voo = 0
    pico = 0

    async def rodar(_item):
        nonlocal em_voo, pico
        em_voo += 1
        pico = max(pico, em_voo)
        await asyncio.sleep(0.01)
        em_voo -= 1

    ordem = [f"n{i}" for i in range(8)]
    await rodar_por_dependencia(ordem, {i: [] for i in ordem}, rodar, limite=3)
    assert pico == 3


async def test_retentativa_acha_o_manifesto_pousado_e_termina(ambiente, monkeypatch):
    """Attempt 1: the manifest lands, the data fails (the pod died mid-upload). Attempt 2, in the SAME
    run: the manifest exists - it must be accepted for being identical, not become
    `objeto_ja_existe` and kill the node forever."""
    primeira = LakeRegistrado(_local(ambiente), falhar_em="dados.parquet")
    _usar_lake(monkeypatch, primeira)
    with pytest.raises(ErroAplicacao):
        await _publicar("gold_e", "ref/teste/e")
    assert any(c.endswith("_manifesto.json") for c in primeira.gravados)

    segunda = LakeRegistrado(_local(ambiente))
    _usar_lake(monkeypatch, segunda)
    await _publicar("gold_e", "ref/teste/e")
    assert segunda.gravados[-1] == "dev/ref/teste/e/_atual.json"


async def test_retentativa_com_dado_diferente_e_conflito(ambiente, monkeypatch):
    lake = LakeRegistrado(_local(ambiente))
    _usar_lake(monkeypatch, lake)
    await _publicar("gold_f", "ref/teste/f")
    # Same run, different content in the snapshot: the immutable parquet cannot be swapped.
    duck = duckdb.connect()
    duck.execute('CREATE TABLE "gold_f" AS SELECT 99 AS id')
    duck.execute(f"COPY \"gold_f\" TO '{engine._dir(EID) / 'gold_f.parquet'}' (FORMAT parquet)")
    try:
        with pytest.raises(ErroAplicacao) as erro:
            await engine._publicar_saida(
                duck,
                _no("gold_f", "ref/teste/f"),
                "gold_f",
                1,
                execucao_id=EID,
                parametros=PARAMETROS,
                tipos_parametro=TIPOS,
            )
    finally:
        duck.close()
    assert erro.value.codigo in ("publicacao_immutavel_divergente", "erro_execucao")
