"""WHOLE extraction in a process (`processos.extrair_em_processo`, without slicing).

What is protected: the result is the SAME as the thread path (`sqlserver._extrair_tabela_sync`):
same columns, same text, same order; zero rows stays `linhas=[]`; data that is not columnar
returns `None` (the caller falls back to the regular path); and a reused connection that died is
replaced once. The gain (decoding outside the worker's GIL) is measured in a real deployment, not here.

No SQL Server: a fake cursor supplies the tuples, and the process pool becomes a thread pool, so
the child function runs inside the test process itself.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from cflow_core.conexoes import processos
from cflow_core.conexoes.conectores import sqlserver

COLUNAS = ["Id", "Valor", "Data", "Nome"]
LINHAS = [
    (1, Decimal("10.50"), dt.datetime(2026, 8, 1, 12, 0), "Á"),
    (2, None, None, "b"),
    (3, Decimal("-0.01"), dt.datetime(2026, 8, 31), None),
]


class Cursor:
    def __init__(self, linhas, colunas=COLUNAS, falhar=False):
        self._linhas = list(linhas)
        self.description = [(c,) for c in colunas]
        self._falhar = falhar

    def execute(self, _sql, _params):
        if self._falhar:
            raise ConnectionResetError("idle connection dropped")

    def fetchmany(self, n):
        lote, self._linhas = self._linhas[:n], self._linhas[n:]
        return lote

    def fetchall(self):
        lote, self._linhas = self._linhas, []
        return lote


class Conn:
    """Each `cursor()` returns the next one in the list (or the only one, if there is just one)."""

    def __init__(self, *cursores):
        self._cursores = list(cursores)
        self.fechada = False

    def cursor(self):
        return self._cursores.pop(0) if len(self._cursores) > 1 else self._cursores[0]

    def close(self):
        self.fechada = True


@pytest.fixture
def pool_em_thread(monkeypatch):
    pool = ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(processos, "_pool", lambda _teto: pool)
    monkeypatch.setattr(processos, "_CONEXOES_FILHO", {})
    yield
    pool.shutdown(wait=True)


def _servir(monkeypatch, fabrica):
    """`_conectar` (the child's) returns a new connection from `fabrica()` on each call."""
    abertas: list[Conn] = []

    def _conectar(*_a, **_k):
        c = fabrica()
        abertas.append(c)
        return c

    monkeypatch.setattr(sqlserver, "_conectar", _conectar)
    return abertas


def _pelo_caminho_em_thread(monkeypatch, linhas):
    @contextlib.contextmanager
    def _conn(_cfg, _abrir, _pool, variante=None):
        yield Conn(Cursor(linhas))

    monkeypatch.setattr(sqlserver, "sqlserver_conn", _conn)
    return sqlserver._extrair_tabela_sync({}, None, "SELECT 1", (), 30, False)


def test_mesmo_resultado_do_caminho_em_thread(monkeypatch, pool_em_thread):
    esperado = _pelo_caminho_em_thread(monkeypatch, LINHAS)
    _servir(monkeypatch, lambda: Conn(Cursor(LINHAS)))
    obtido = processos.extrair_em_processo({}, None, "SELECT 1", [], 30, 2)
    assert obtido.colunas == esperado.colunas
    assert obtido.tabela.equals(esperado.tabela)


def test_zero_linhas_continua_lista_vazia(monkeypatch, pool_em_thread):
    esperado = _pelo_caminho_em_thread(monkeypatch, [])
    _servir(monkeypatch, lambda: Conn(Cursor([])))
    obtido = processos.extrair_em_processo({}, None, "SELECT 1", [], 30, 2)
    assert obtido.linhas == esperado.linhas == []
    assert obtido.tabela is None


def test_coluna_sem_nome_colunar_devolve_none(monkeypatch, pool_em_thread):
    _servir(monkeypatch, lambda: Conn(Cursor(LINHAS)))
    # The criterion is the columnar one; when it refuses, the return is None and the parent queries again.
    monkeypatch.setattr(processos.colunar, "nomes_colunares", lambda _c: False)
    assert processos.extrair_em_processo({}, None, "SELECT 1", [], 30, 2) is None


def test_conexao_reaproveitada_que_morreu_e_trocada_uma_vez(monkeypatch, pool_em_thread):
    conexoes = iter([Conn(Cursor(LINHAS), Cursor([], falhar=True)), Conn(Cursor(LINHAS))])
    abertas = _servir(monkeypatch, lambda: next(conexoes))
    primeira = processos.extrair_em_processo({"host": "h"}, "s", "SELECT 1", [], 30, 2)
    # Second extraction: the stored connection has a cursor that fails on execute, so a new connection is made.
    segunda = processos.extrair_em_processo({"host": "h"}, "s", "SELECT 1", [], 30, 2)
    assert primeira.tabela.num_rows == segunda.tabela.num_rows == 3
    assert (
        len(abertas) == 2
    )  # the first connection was reused; the replacement opened just one more


def test_conector_so_usa_o_processo_com_a_setting(monkeypatch):
    chamadas = []

    def _em_processo(*a):
        chamadas.append(a)
        return processos.Extracao(["x"], linhas=[])

    monkeypatch.setattr(processos, "extrair_em_processo", _em_processo)
    monkeypatch.setattr(sqlserver, "_teto_de_processos", lambda _cfg: 4)
    monkeypatch.setattr(
        sqlserver, "_extrair_tabela_sync", lambda *a: processos.Extracao(["y"], linhas=[])
    )

    import asyncio

    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_EXTRACAO_EM_PROCESSO", "false")
    get_settings.cache_clear()
    r = asyncio.run(sqlserver.ConectorSqlServer().extrair_tabela({}, None, "SELECT 1"))
    assert r.colunas == ["y"] and not chamadas
    monkeypatch.setenv("CFLOW_EXTRACAO_EM_PROCESSO", "true")
    get_settings.cache_clear()
    r = asyncio.run(sqlserver.ConectorSqlServer().extrair_tabela({}, None, "SELECT 1"))
    assert r.colunas == ["x"] and len(chamadas) == 1
    assert "OPTION (RECOMPILE)" in chamadas[0][2]
    get_settings.cache_clear()


def test_erro_do_filho_volta_como_dado_e_nao_quebra_o_pool(monkeypatch, pool_em_thread):
    """An exception that cannot be rebuilt by `pickle` would break the whole pool
    (`BrokenProcessPool`). The child returns it as text; the parent raises a plain error, and the
    connection cache is cleared."""

    class ErroSemArgs(Exception):
        def __init__(self, obrigatorio):
            super().__init__("invalid sql")

    class CursorQueFalha(Cursor):
        def execute(self, _sql, _params):
            raise ErroSemArgs(1)

    _servir(monkeypatch, lambda: Conn(CursorQueFalha(LINHAS)))
    with pytest.raises(RuntimeError, match="invalid sql"):
        processos.extrair_em_processo({}, None, "SELECT 1", [], 30, 2)
    assert processos._CONEXOES_FILHO == {}


def test_timeout_de_consulta_nao_e_repetido(monkeypatch, pool_em_thread):
    tentativas = []

    class CursorLento(Cursor):
        def execute(self, _sql, _params):
            tentativas.append(1)
            raise TimeoutError("query timeout")

    _servir(monkeypatch, lambda: Conn(CursorLento(LINHAS)))
    with pytest.raises(RuntimeError, match="TimeoutError"):
        processos.extrair_em_processo({}, None, "SELECT 1", [], 30, 2)
    assert len(tentativas) == 1


def test_pool_spawn_real_devolve_o_erro_e_segue_servindo():
    """With the real PROCESS pool (`spawn`, pickle both ways): a connection refused in the child
    comes back as a plain error, and the next extraction still finds the pool alive."""
    processos.encerrar()
    try:
        cfg = {"host": "127.0.0.1", "porta": 1, "base": "x", "usuario": "u"}
        for _ in range(2):
            with pytest.raises(RuntimeError, match="in-process extraction failed"):
                processos.extrair_em_processo(cfg, "s", "SELECT 1", [], 5, 2)
        assert processos._POOL is not None
    finally:
        processos.encerrar()
