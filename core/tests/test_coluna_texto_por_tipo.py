"""A single-type `coluna_texto` produces the SAME text as `texto(v)` value by value, which keeps
columnar extraction byte-identical to the regular path."""

import random
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest

from cflow_core.conexoes import colunar as C

rnd = random.Random(20260928)


def _decimais(n):
    out = [Decimal("0.00"), Decimal("-0.00"), Decimal("-0"), Decimal("123456789012345678.99")]
    for _ in range(n):
        esc = rnd.choice([0, 2, 4, 10])
        out.append(Decimal(rnd.randint(-(10**18), 10**18)).scaleb(-esc))
    return out


def _datas(n):
    base = datetime(1900, 1, 1)
    out = [datetime(2026, 9, 28, 12, 0, 0, 123456), datetime(2026, 9, 28, tzinfo=UTC)]
    out += [base + timedelta(seconds=rnd.randint(0, 5_000_000_000)) for _ in range(n)]
    return out


CASOS = {
    "int": [0, -1, 2**63, *(rnd.randint(-(10**12), 10**12) for _ in range(500))],
    "float": [
        0.0,
        -0.0,
        1e-300,
        1.2428500000000001e3,
        *(rnd.uniform(-1e9, 1e9) for _ in range(500)),
    ],
    "decimal": _decimais(500),
    "datetime": _datas(500),
    "date": [date(1900, 1, 1) + timedelta(days=rnd.randint(0, 60000)) for _ in range(500)],
    "time": [
        time(rnd.randint(0, 23), rnd.randint(0, 59), rnd.randint(0, 59), rnd.randint(0, 999999))
        for _ in range(200)
    ],
    "uuid": [uuid.UUID(int=rnd.getrandbits(128)) for _ in range(200)],
}


@pytest.mark.parametrize("nome", sorted(CASOS))
@pytest.mark.parametrize("com_nulo", [False, True])
def test_tipo_unico_igual_ao_texto_por_valor(nome, com_nulo):
    valores = list(CASOS[nome])
    if com_nulo:
        valores[1::7] = [None] * len(valores[1::7])
    esperado = [C.texto(v) for v in valores]
    assert C.coluna_texto(valores) == esperado


def test_bool_e_misturas_seguem_pelo_texto():
    for valores in (
        [True, False, None],
        [1, True],
        [1, 1.5],
        [date(2026, 1, 1), datetime(2026, 1, 1)],
    ):
        assert C.coluna_texto(valores) == [C.texto(v) for v in valores]


@pytest.mark.parametrize("ruim", [float("nan"), float("inf"), Decimal("NaN"), Decimal("-Infinity")])
def test_nao_finito_continua_fora_do_colunar(ruim):
    tipo = type(ruim)
    ok = 1.5 if tipo is float else Decimal("1.5")
    with pytest.raises(C.ForaDoColunar):
        C.coluna_texto([ok, ruim])
