"""xlsx generated in a SEPARATE PROCESS (`relatorios/processo_xlsx.py`).

The child runs the same `gerar_xlsx`: the file must come out equal to the thread's (byte for byte
in every zip member except `docProps/core.xml`, which carries the write instant). What crosses the
process boundary -- progress and error -- has its own contract.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.relatorios import gerar, processo_xlsx

N = 2500


def _definicao() -> dict:
    colunas = [
        {"coluna": "txt", "rotulo": "Text"},
        {"coluna": "valor", "formato": "moeda"},
        {"coluna": "dia", "formato": "data"},
    ]
    return {
        "fonte": {"pipeline": str(uuid.uuid4()), "no": "x"},
        "colunas": colunas,
        "aba": "Base",
        "carimbo": False,
    }


def _snapshot(tmp_path):
    snap = tmp_path / "snap.parquet"
    tabela = pa.table(
        {
            "txt": [f"row {i} &<>" if i % 7 else None for i in range(N)],
            "valor": [Decimal(i) / 100 for i in range(N)],
            "dia": [dt.date(2026, 1, 1) + dt.timedelta(days=i % 400) for i in range(N)],
        }
    )
    pq.write_table(tabela, snap)
    return snap


def _membros(caminho):
    with zipfile.ZipFile(caminho) as z:
        return {n: z.read(n) for n in z.namelist() if n != "docProps/core.xml"}


def test_processo_gera_os_mesmos_bytes_que_a_thread(tmp_path):
    snap, definicao = _snapshot(tmp_path), _definicao()
    try:
        na_thread = gerar.gerar_xlsx(snap, definicao, tmp_path / "thread.xlsx")
        progresso = tmp_path / "progresso"
        no_processo = asyncio.run(
            processo_xlsx.gerar(
                str(snap), definicao, tmp_path / "processo.xlsx", progresso_em=str(progresso)
            )
        )
        # It really ran in a child: the pool is still alive and has a process. Had it broken, the
        # generation would silently fall back to the thread and the rest of the test would pass
        # all the same.
        pool = processo_xlsx._POOL
        assert pool is not None and pool._processes, "the generation did not go through the child"
    finally:
        processo_xlsx.descartar_pool()
    # `bytes` is OUTSIDE the comparison: it is the size of the whole zip, and the
    # `docProps/core.xml` openpyxl writes carries the generation time -- two generations in
    # different seconds give different sizes, which made the test flaky. The equality the pool must
    # prove is that of the zip MEMBERS, below, which is byte for byte and already excludes
    # `core.xml`.
    assert no_processo["linhas"] == na_thread["linhas"]
    assert _membros(tmp_path / "processo.xlsx") == _membros(tmp_path / "thread.xlsx")
    assert processo_xlsx.ler_progresso(progresso) == N  # the parent's heartbeat reads from here


def test_erro_comum_atravessa_o_processo(tmp_path):
    try:
        with pytest.raises(ValueError, match="unknown writer"):
            asyncio.run(
                processo_xlsx.gerar(
                    str(_snapshot(tmp_path)),
                    _definicao(),
                    tmp_path / "x.xlsx",
                    progresso_em=str(tmp_path / "p"),
                    escritor="nenhum",
                )
            )
    finally:
        processo_xlsx.descartar_pool()


@pytest.mark.parametrize(
    ("concorrencia", "cpus", "esperado"),
    [(4, 4, 2), (4, 16, 4), (2, 16, 2), (4, 1, 1), (4, 3, 1)],
)
def test_pool_usa_no_maximo_metade_das_cpus(monkeypatch, concorrencia, cpus, esperado):
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_RELATORIOS_CONCORRENCIA", str(concorrencia))
    get_settings.cache_clear()
    monkeypatch.setattr(
        processo_xlsx.os, "sched_getaffinity", lambda _p: set(range(cpus)), raising=False
    )
    monkeypatch.setattr(
        "cflow_core.duck.cpus_cgroup", lambda: None
    )  # no quota: does not depend on the host
    try:
        assert processo_xlsx.tamanho_do_pool() == esperado
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(("cota", "esperado"), [(4.0, 2), (2.0, 1), (2.5, 1), (8.0, 4)])
def test_pool_respeita_a_cota_de_cpu_do_cgroup(monkeypatch, cota, esperado):
    # Kubernetes CPU limit on a large node: affinity says 32, the quota rules.
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_RELATORIOS_CONCORRENCIA", "4")
    get_settings.cache_clear()
    monkeypatch.setattr(
        processo_xlsx.os, "sched_getaffinity", lambda _p: set(range(32)), raising=False
    )
    monkeypatch.setattr("cflow_core.duck.cpus_cgroup", lambda: cota)
    try:
        assert processo_xlsx.tamanho_do_pool() == esperado
    finally:
        get_settings.cache_clear()


def test_descartar_um_pool_velho_nao_derruba_o_novo(monkeypatch):
    class _Pool:
        def __init__(self):
            self.desligado = False

        def shutdown(self, wait, cancel_futures):
            self.desligado = True

    velho, novo = _Pool(), _Pool()
    monkeypatch.setattr(processo_xlsx, "_POOL", novo)
    processo_xlsx.descartar_pool(velho)  # the late BrokenProcessPool of the old pool
    assert velho.desligado and not novo.desligado
    assert processo_xlsx._POOL is novo
    processo_xlsx.descartar_pool(novo)
    assert novo.desligado and processo_xlsx._POOL is None


def _quebra(*_a, **_k):
    raise ErroAplicacao(codigo="layout_x", mensagem="Error message.", status=422)


def test_erro_de_aplicacao_volta_como_dado(monkeypatch):
    # The default pickle would rebuild the exception with the message only, and __init__ also
    # requires the code.
    monkeypatch.setattr(gerar, "gerar_xlsx", _quebra)
    assert processo_xlsx._gerar_no_filho() == ("erro", ("layout_x", "Error message.", 422))


def test_erro_de_aplicacao_e_levantado_de_novo_no_pai(monkeypatch, tmp_path):
    monkeypatch.setattr(gerar, "gerar_xlsx", _quebra)
    executor = ThreadPoolExecutor(1)  # same contract as the pool, without starting a process
    monkeypatch.setattr(processo_xlsx, "_pool", lambda: executor)
    with pytest.raises(ErroAplicacao) as erro:
        asyncio.run(processo_xlsx.gerar(progresso_em=str(tmp_path / "p")))
    executor.shutdown()
    assert (erro.value.codigo, erro.value.status) == ("layout_x", 422)
