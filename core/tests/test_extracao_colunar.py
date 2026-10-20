"""Columnar SQL Server extraction (`conexoes/colunar.py`): it must produce EXACTLY the table the
regular path produces, i.e. `amostra` (`serial` per value -> list of dicts) + `engine._carregar`.

Every test here loads the same rows through both paths, with a fake cursor that returns what
`pytds` would (`as_dict=True`: one dict per row, an empty name becomes the index, a repeated name
keeps the last), and compares table with table."""

from __future__ import annotations

import asyncio
import random
import threading
import types
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import duckdb
import pytest

from cflow_core.conexoes import colunar
from cflow_core.conexoes.conectores import sqlserver
from cflow_core.conexoes.conectores.base import serial
from cflow_core.execucao import engine

VALORES = [
    None,
    "",
    "00123456789",
    'Café \n\t"x"\\ ',
    "日本",
    0,
    1,
    -7,
    2**63 + 5,
    10**30,
    1.5,
    1e-05,
    0.1,
    1e20,
    1e16,
    -0.0,
    123456789.123456789,
    True,
    False,
    Decimal("1253.18"),
    Decimal("1E+2"),
    Decimal("-0.00"),
    Decimal("0.0000001"),
    Decimal("12345678901234567890.123"),
    datetime(2026, 8, 6, 12, 17, 23),
    datetime(2026, 8, 6, 12, 17, 23, 120000),
    datetime(2026, 8, 6, 12, 17, 23, tzinfo=timezone(timedelta(hours=-3))),
    date(2026, 8, 6),
    time(12, 1, 2),
    time(0, 0, 0, 5),
    uuid.UUID(int=5),
    bytes([0, 97, 98]),
    bytearray(b"ab"),
]


class _Cursor:
    def __init__(self, colunas, linhas, registro, como_dict=True):
        self._colunas, self._linhas, self._registro = colunas, linhas, registro
        self._como_dict = como_dict
        self._pos = 0
        # pytds `dict_row_strategy`: an empty name becomes the index; for a repeated name the last wins.
        self._nomes = [(n or str(i)) for i, n in enumerate(colunas)]

    def execute(self, sql, params=()):
        self._registro.append(sql)
        self._pos = 0

    @property
    def description(self):
        return [(c, None, None, None, None, None, None) for c in self._colunas]

    def _fatia(self, n):
        fatia = self._linhas[self._pos : self._pos + n]
        self._pos += len(fatia)
        if not self._como_dict:
            return [tuple(t) for t in fatia]
        return [dict(zip(self._nomes, t, strict=True)) for t in fatia]

    def fetchall(self):
        return self._fatia(len(self._linhas))

    def fetchmany(self, n=1):
        return self._fatia(n)


class _Conexao:
    def __init__(self, *a):
        self._a = a

    def cursor(self):
        return _Cursor(*self._a)

    def close(self):
        pass


@pytest.fixture
def fonte(monkeypatch):
    def _instalar(colunas, linhas):
        registro: list[str] = []
        monkeypatch.setattr(
            sqlserver,
            "_conectar",
            lambda *a, **k: _Conexao(colunas, linhas, registro, k.get("as_dict", True)),
        )
        monkeypatch.setattr(
            sqlserver,
            "get_settings",
            lambda: types.SimpleNamespace(
                conexao_query_timeout_segundos=60,
                conexao_amostra_timeout_segundos=30,
                pool_fontes=False,
            ),
        )
        return registro

    return _instalar


def _tabela(carregar):
    d = duckdb.connect()
    carregar(d)
    desc = [(r[0], r[1]) for r in d.execute('DESCRIBE "t"').fetchall()]
    return desc, d.execute('SELECT * FROM "t"').fetchall()


def _pelo_caminho_de_sempre():
    cols, linhas = asyncio.run(sqlserver.ConectorSqlServer().amostra({}, None, "SELECT x", None))
    return _tabela(lambda d: engine._carregar(d, "t", linhas, cols))


def _pelo_colunar():
    ex = asyncio.run(sqlserver.ConectorSqlServer().extrair_tabela({}, None, "SELECT x"))
    return ex, _tabela(lambda d: engine._carregar_extracao(d, "t", ex))


def test_texto_e_serial_seguido_do_json_valor_a_valor():
    for v in VALORES:
        assert colunar.coluna_texto([v]) == engine._texto_como_json([serial(v)]), v


@pytest.mark.parametrize("v", [float("nan"), float("inf"), Decimal("NaN"), [1], {"k": 1}])
def test_o_que_o_json_trataria_levanta(v):
    with pytest.raises(colunar.ForaDoColunar):
        colunar.coluna_texto(["x", v])


def test_cada_tipo_do_driver_sai_igual(fonte):
    fonte(["v", "fixo"], [(v, "x") for v in VALORES])
    ex, tabela = _pelo_colunar()
    assert ex.tabela is not None, (
        "fell back to the regular path with data the columnar path reproduces"
    )
    assert tabela == _pelo_caminho_de_sempre()


def test_aleatorio_em_varios_lotes(fonte, monkeypatch):
    monkeypatch.setattr(colunar, "LOTE_LINHAS", 7)
    rnd = random.Random(20260911)
    colunas = [f"c{i}" for i in range(12)] + ["Id", "UNIT_BLK_LT", "name with space"]
    fonte(colunas, [tuple(rnd.choice(VALORES) for _ in colunas) for _ in range(403)])
    ex, tabela = _pelo_colunar()
    assert ex.tabela is not None and ex.tabela.num_rows == 403
    assert tabela == _pelo_caminho_de_sempre()


def test_nome_vazio_e_repetido_como_o_driver(fonte):
    # "" becomes column "1" in the driver dict and falls into `_colunas_validas`; repeated "a": the last wins.
    fonte(["a", "", "b", "a"], [(1, 2, 3, 4), (None, "x", Decimal("1.5"), "z")])
    ex, tabela = _pelo_colunar()
    assert ex.tabela is not None
    assert tabela == _pelo_caminho_de_sempre()
    assert tabela[1] == [("4", "3"), ("z", "1.5")]


def test_nome_que_quebra_a_spec_usa_o_caminho_de_sempre_sem_reconsultar(fonte):
    registro = fonte(['com"aspas', "b"], [("1", 2)])
    ex, tabela = _pelo_colunar()
    assert ex.tabela is None and len(registro) == 1
    assert tabela == _pelo_caminho_de_sempre()


def test_nan_volta_ao_caminho_de_sempre_reconsultando(fonte):
    registro = fonte(["v", "w"], [(1.5, "a"), (float("nan"), "b")])
    ex = asyncio.run(sqlserver.ConectorSqlServer().extrair_tabela({}, None, "SELECT x"))
    assert ex.tabela is None and len(registro) == 2
    assert (
        ex.linhas
        == asyncio.run(sqlserver.ConectorSqlServer().amostra({}, None, "SELECT x", None))[1]
    )


def test_sem_linhas_mantem_o_schema_como_antes(fonte):
    fonte(["a", "b"], [])
    ex, tabela = _pelo_colunar()
    assert ex.linhas == [] and tabela == _pelo_caminho_de_sempre()
    assert tabela == ([("a", "VARCHAR"), ("b", "VARCHAR")], [])


def test_amostra_converte_fora_do_event_loop(fonte, monkeypatch):
    fonte(["v"], [(Decimal("1.5"),), (Decimal("2.5"),)])
    threads = set()
    original = sqlserver.serial

    def _espia(v):
        threads.add(threading.current_thread() is threading.main_thread())
        return original(v)

    monkeypatch.setattr(sqlserver, "serial", _espia)
    asyncio.run(sqlserver.ConectorSqlServer().amostra({}, None, "SELECT x", None))
    assert threads == {False}, "the per-value conversion ran on the event loop"
