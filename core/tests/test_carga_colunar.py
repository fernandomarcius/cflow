"""Columnar load of the connection source (`engine._carregar`): it must produce EXACTLY what the
JSON path produced: same columns, all VARCHAR, same text, same NULL.

The JSON path (`_carregar_json`) is still in the code and is the yardstick: every test here loads
the same rows through both and compares table to table. What the columnar path cannot reproduce
(list, dict, NaN, a column name that breaks the spec) it REFUSES, and the load falls back to JSON."""

from __future__ import annotations

import random
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import duckdb
import pytest

from cflow_core.execucao import engine


def _tabela(carregar, linhas, colunas=None):
    d = duckdb.connect()
    carregar(d, "t", linhas, colunas)
    desc = [(r[0], r[1]) for r in d.execute('DESCRIBE "t"').fetchall()]
    dados = d.execute('SELECT * FROM "t"').fetchall()
    return desc, dados


def _pelo_json(d, nome, linhas, colunas=None):
    d.execute(f'DROP TABLE IF EXISTS "{nome}"')
    cols = engine._colunas_validas(colunas or list(linhas[0].keys()))
    engine._carregar_json(d, nome, linhas, cols)


VALORES = [
    None,
    "",
    "01234567890",
    'Caf\u00e9 \n\t"x"\\ ',
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
    datetime(2026, 8, 6, 12, 17, 23),
    datetime(2026, 8, 6, 12, 17, 23, 120000),
    datetime(2026, 8, 6, 12, 17, 23, tzinfo=timezone(timedelta(hours=-3))),
    date(2026, 8, 6),
    time(12, 1, 2),
    time(0, 0, 0, 5),
    uuid.UUID(int=5),
    bytes([0, 97, 98]),
]


def test_each_driver_type_matches_the_json_path(monkeypatch):
    linhas = [{"v": v, "fixo": "x"} for v in VALORES]
    esperado = _tabela(_pelo_json, linhas)

    # and it was the COLUMNAR path that loaded it, not JSON behind the scenes
    def _proibido(*a, **k):
        raise AssertionError("fell to the JSON path with data the columnar path reproduces")

    monkeypatch.setattr(engine, "_carregar_json", _proibido)
    assert _tabela(engine._carregar, linhas) == esperado


def test_aleatorio_muitas_colunas_mistas_igual_ao_caminho_json():
    rnd = random.Random(20260911)
    colunas = [f"c{i}" for i in range(12)] + ["Id", "UNIT_BLOCK_LOT", "name with space"]
    linhas = [{c: rnd.choice(VALORES) for c in colunas} for _ in range(400)]
    assert _tabela(engine._carregar, linhas, colunas) == _tabela(_pelo_json, linhas, colunas)


def test_colunas_do_driver_mandam_na_ordem_e_ausente_vira_null():
    linhas = [{"b": "1", "a": 2}, {"a": 3}]
    colunas = [
        "a",
        "b",
        "",
        "None",
        "a",
        "c",
    ]  # empty, "None" and repeated ones are dropped; "c" does not exist
    esperado = _tabela(_pelo_json, linhas, colunas)
    assert _tabela(engine._carregar, linhas, colunas) == esperado
    assert [n for n, _ in esperado[0]] == ["a", "b", "c"]
    assert all(t == "VARCHAR" for _, t in esperado[0])


@pytest.mark.parametrize(
    "linhas",
    [
        [{"v": float("nan")}],
        [{"v": float("inf")}],
        [{"v": [1, 2]}],
        [{"v": {"k": 1}}],
        [{"v": bytearray(b"ab")}],
        [{'com"aspas': "1"}],
        [{"A": "1", "a": "2"}],
    ],
    ids=["nan", "inf", "lista", "dict", "bytearray", "aspas-no-nome", "caixa"],
)
def test_o_que_o_colunar_nao_garante_volta_ao_json(linhas):
    d = duckdb.connect()
    assert (
        engine._carregar_colunar(d, "t", linhas, engine._colunas_validas(list(linhas[0]))) is False
    )
    assert (
        d.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='t'").fetchone()[
            0
        ]
        == 0
    )
    try:
        esperado = _tabela(_pelo_json, linhas)
    except Exception as exc:  # JSON cannot load it either: the error is the same in both
        with pytest.raises(type(exc)):
            _tabela(engine._carregar, linhas)
        return
    assert _tabela(engine._carregar, linhas) == esperado


def test_vazio_continua_como_antes():
    assert _tabela(engine._carregar, [], ["a", "b"]) == ([("a", "VARCHAR"), ("b", "VARCHAR")], [])
    assert _tabela(engine._carregar, [], None) == ([("vazio", "INTEGER")], [])
