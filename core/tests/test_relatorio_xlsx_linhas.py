"""xlsx row writer (`relatorios/celulas_xlsx.py`) -- the file must come out IDENTICAL to the
cell-by-cell openpyxl one (`escritor="openpyxl"`, the reference path).

"Identical" means byte for byte in every zip member except `docProps/core.xml`, which carries the
write instant. The scenarios cover what decides a byte: value type (text with leading/trailing
spaces, `&<>`, `\\r`, formula `=`, error code, empty, 17-digit number, `-0`, Decimal, date,
datetime, boolean, null), the three cell paths (raw, `number_format`, bold/font), the style
registration ORDER (a raw date before a new formatted column on the same row), a trailing empty
column, two header rows, a fixed-block preamble and splitting into parts."""

from __future__ import annotations

import datetime as dt
import random
import uuid
import zipfile
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from cflow_core.relatorios import gerar


def _gerar_os_dois(tmp_path, tabela, definicao, **kw):
    snap = tmp_path / "snap.parquet"
    pq.write_table(tabela, snap)
    saidas = {}
    for escritor in ("openpyxl", "colunar"):
        destino = tmp_path / f"{escritor}.xlsx"
        r = gerar.gerar_xlsx(snap, definicao, destino, escritor=escritor, **kw)
        saidas[escritor] = (destino, r)
    return saidas


def _membros(caminho):
    with zipfile.ZipFile(caminho) as z:
        return {n: z.read(n) for n in z.namelist() if n != "docProps/core.xml"}


def _iguais(saidas):
    (a, ra), (b, rb) = saidas["openpyxl"], saidas["colunar"]
    assert ra == {**rb, "bytes": ra["bytes"]}, "rows/sheets differ"
    ma, mb = _membros(a), _membros(b)
    assert list(ma) == list(mb)
    for nome in ma:
        assert ma[nome] == mb[nome], f"{nome} differs"


def _definicao(colunas, **extra):
    return {"fonte": {"pipeline": str(uuid.uuid4()), "no": "x"}, "colunas": colunas, **extra}


TEXTOS = ["a", " b ", "a&b<c>d", "x\ry", "\tlead", "=SUM(A1)", "=", "#N/A", "", "日本", "x\ny"]


def _tabela_mista(n: int, semente: int = 20260911) -> pa.Table:
    rnd = random.Random(semente)

    def talvez(v):
        return None if rnd.random() < 0.15 else v

    return pa.table(
        {
            "dt": [
                talvez(dt.datetime(2026, 1, 1) + dt.timedelta(seconds=rnd.randint(0, 9**7)))
                for _ in range(n)
            ],
            "valor": [talvez(Decimal(rnd.randint(-(10**7), 10**7)) / 100) for _ in range(n)],
            "txt": [talvez(rnd.choice(TEXTOS)) for _ in range(n)],
            "inteiro": [
                talvez(rnd.choice([0, 1, -7, 12345678901234567, 2**53 + 1])) for _ in range(n)
            ],
            "dbl": [
                talvez(rnd.choice([0.1 + 0.2, -0.0, 1242.8500000000001, 1e-5, 1e20, float("nan")]))
                for _ in range(n)
            ],
            "dia": [
                talvez(dt.date(1900, 1, 1) + dt.timedelta(days=rnd.randint(0, 50000)))
                for _ in range(n)
            ],
            "flag": [talvez(rnd.random() < 0.5) for _ in range(n)],
            "tz": pa.array(
                [talvez(dt.datetime(2026, 8, 6, 12, 17, tzinfo=dt.UTC)) for _ in range(n)],
                pa.timestamp("us", tz="UTC"),
            ),
        }
    )


def test_tipos_e_caminhos_de_celula_iguais(tmp_path):
    colunas = [
        {"coluna": "dt"},  # raw date: automatic style, registered at WRITE time
        {"coluna": "valor", "formato": "moeda"},  # format created BEFORE the write
        {"coluna": "txt", "rotulo": "Texto"},
        {"coluna": "inteiro", "formato": "inteiro", "negrito": True},
        {"coluna": "dbl"},
        {"coluna": "dia", "formato": "data"},
        {"coluna": "flag", "negrito": True},
        {"coluna": "tz", "formato": "percentual"},  # date in a column with a NON-date format
    ]
    _iguais(_gerar_os_dois(tmp_path, _tabela_mista(700), _definicao(colunas, aba="Base")))


def test_fonte_tamanho_duas_linhas_de_cabecalho_e_coluna_vazia(tmp_path):
    colunas = [
        {"coluna": "txt", "rotulo": "T", "rotulo2": "t2"},
        {"coluna": "valor", "formato": "moeda", "rotulo2": "v2"},
        {"coluna": "dia"},
    ]
    definicao = _definicao(
        colunas, linhas_cabecalho=2, colunas_vazias_no_fim=2, fonte_tamanho=9, carimbo=False
    )
    _iguais(_gerar_os_dois(tmp_path, _tabela_mista(300), definicao))


def test_divisao_em_partes(tmp_path):
    colunas = [{"coluna": "inteiro", "formato": "inteiro"}, {"coluna": "txt"}, {"coluna": "dt"}]
    saidas = _gerar_os_dois(
        tmp_path, _tabela_mista(1003), _definicao(colunas, aba="Dados"), teto_linhas=250
    )
    assert saidas["colunar"][1]["abas"][:5] == [f"Dados (parte {i})" for i in range(1, 6)]
    _iguais(saidas)


def test_preambulo_do_fluxo_futuro(tmp_path):
    colunas = [
        {"coluna": "txt", "rotulo": ""},
        {"coluna": "valor", "formato": "moeda", "rotulo": "RECEBER"},
        {"coluna": "dbl", "formato": "moeda", "rotulo": "INAD"},
    ]
    definicao = _definicao(
        colunas,
        titulo={"texto": "Report {{param.project_name}}", "ate_coluna": "C"},
        linhas_fixas=[{"linha": 2, "coluna": 1, "texto": "average"}],
        totais={"linha": 4, "colunas": ["valor", "dbl"]},
        primeira_linha_dado=5,
        linha_cabecalho=3,
        autofiltro=False,
    )
    _iguais(
        _gerar_os_dois(
            tmp_path, _tabela_mista(120), definicao, parametros={"project_name": "PROJECT X"}
        )
    )


def test_tipos_raros_texto_longo_e_linha_toda_nula(tmp_path):
    # time/timedelta/bytes/list/struct become TEXT in `_celula`; text above the Excel cap is cut at
    # 32,767; a Unicode space at the ends needs `xml:space`; the 3rd row is all null.
    tabela = pa.table(
        {
            "hora": pa.array([dt.time(1, 2, 3), dt.time(0, 0, 0, 5), None], pa.time64("us")),
            "dur": pa.array([dt.timedelta(days=1, seconds=5), dt.timedelta(0), None]),
            "bin": pa.array([b"\x00ab", b"", None], pa.binary()),
            "lista": pa.array([[1, 2], [], None], pa.list_(pa.int64())),
            "obj": pa.array([{"a": 1}, {"a": None}, None], pa.struct([("a", pa.int64())])),
            "longo": ["x" * 40000, " nbsp ", None],
            "dec": pa.array(
                [Decimal("12345678901234567890.123"), Decimal("-0.00"), None], pa.decimal128(38, 3)
            ),
        }
    )
    colunas = [{"coluna": c} for c in tabela.column_names]
    colunas[-1]["formato"] = "moeda"
    _iguais(_gerar_os_dois(tmp_path, tabela, _definicao(colunas)))


def test_sem_linhas(tmp_path):
    vazia = _tabela_mista(5).slice(0, 0)
    colunas = [{"coluna": "txt"}, {"coluna": "valor", "formato": "moeda"}]
    _iguais(_gerar_os_dois(tmp_path, vazia, _definicao(colunas)))


def test_caractere_de_controle_continua_erro(tmp_path):
    from openpyxl.utils.exceptions import IllegalCharacterError

    tabela = pa.table({"txt": ["ok", "bell\x07"]})
    snap = tmp_path / "s.parquet"
    pq.write_table(tabela, snap)
    for escritor in ("openpyxl", "colunar"):
        with pytest.raises(IllegalCharacterError):
            gerar.gerar_xlsx(
                snap,
                _definicao([{"coluna": "txt"}]),
                tmp_path / f"{escritor}.xlsx",
                escritor=escritor,
            )
