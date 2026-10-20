"""Report layout: what the layer accepts and, above all, what it refuses BY NAME.
No database, no Parquet -- only structural validation and the check against a list of columns."""

from __future__ import annotations

import uuid

import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.relatorios import layout

PIPELINE = str(uuid.uuid4())


def _layout(**extra) -> dict:
    base = {
        "nome": "Report 1 - Net present value",
        "fonte": {"pipeline": PIPELINE, "no": "full_portfolio_node"},
        "aba": "REPORT 1. NET PRESENT VALUE",
        "colunas": [
            {"coluna": "Company", "rotulo": "Company"},
            {"coluna": "Sale_Date", "rotulo": "Sale Date", "formato": "data"},
            {"coluna": "Amount", "rotulo": "Value", "formato": "moeda"},
            {"coluna": "Rate_Pct", "rotulo": "Rate %", "formato": "percentual_pontos"},
        ],
        "cabecalho_fixo": True,
        "autofiltro": True,
        "carimbo": True,
    }
    base.update(extra)
    return base


def _codigo(fn, *a, **kw) -> str:
    with pytest.raises(ErroAplicacao) as exc:
        fn(*a, **kw)
    assert exc.value.status == 400
    assert exc.value.mensagem and "?" not in exc.value.codigo
    return exc.value.codigo


def test_layout_valido_passa():
    layout.validar(_layout())


def test_formato_fora_do_vocabulario_e_erro():
    d = _layout()
    d["colunas"][2]["formato"] = "dividir_por_100"
    assert _codigo(layout.validar, d) == "layout_formato_invalido"


@pytest.mark.parametrize("chave", ["sql", "formula", "total", "linha_total", "agregacao", "filtro"])
def test_sql_formula_ou_total_no_layout_e_recusado_com_nome(chave):
    d = _layout()
    d[chave] = "SELECT 1"
    assert _codigo(layout.validar, d) == "layout_transforma_dado"


def test_total_dentro_da_coluna_tambem_e_recusado():
    d = _layout()
    d["colunas"][2]["total"] = "soma"
    assert _codigo(layout.validar, d) == "layout_transforma_dado"


def test_rotulo_que_e_formula_do_excel_e_recusado():
    d = _layout()
    d["colunas"][0]["rotulo"] = "=SUM(A2:A999)"
    assert _codigo(layout.validar, d) == "layout_transforma_dado"


def test_chave_desconhecida_nao_e_ignorada():
    d = _layout(cor_de_fundo="azul")
    assert _codigo(layout.validar, d) == "layout_chave_desconhecida"


def test_coluna_repetida_e_erro():
    d = _layout()
    d["colunas"].append({"coluna": "Company"})
    assert _codigo(layout.validar, d) == "layout_coluna_repetida"


def test_fonte_sem_pipeline_ou_no_e_erro():
    assert _codigo(layout.validar, _layout(fonte={"pipeline": PIPELINE})) == "layout_fonte_invalida"
    assert (
        _codigo(layout.validar, _layout(fonte={"pipeline": "not-a-uuid", "no": "x"}))
        == "layout_fonte_invalida"
    )


def test_sem_colunas_e_erro():
    assert _codigo(layout.validar, _layout(colunas=[])) == "layout_sem_colunas"


def test_coluna_inexistente_no_snapshot_e_erro_e_nomeia_a_coluna():
    d = _layout()
    codigo = _codigo(layout.validar_contra_snapshot, d, ["Company", "Sale_Date", "Amount"])
    assert codigo == "layout_coluna_inexistente"
    with pytest.raises(ErroAplicacao) as exc:
        layout.validar_contra_snapshot(d, ["Company"])
    assert "'Rate_Pct'" in exc.value.mensagem and "'Sale_Date'" in exc.value.mensagem


def test_coluna_do_snapshot_fora_do_layout_nao_sai_e_nao_e_erro():
    d = _layout()
    layout.validar_contra_snapshot(d, [c["coluna"] for c in d["colunas"]] + ["coluna_extra"])
    assert layout.colunas_do_layout(d) == [
        "Company",
        "Sale_Date",
        "Amount",
        "Rate_Pct",
    ]


def test_formatos_sao_number_format_e_nunca_conversao():
    assert layout.FORMATOS["percentual_pontos"] == '0.00"%"'
    assert layout.FORMATOS["percentual"] == "0.00%"
    assert set(layout.FORMATOS) == {
        "moeda",
        # No space after the currency symbol. Own entry because the vocabulary is closed --
        # reusing `moeda` would change the cell of other layouts.
        "moeda_colada",
        "data",
        "percentual",
        "percentual_pontos",
        # Number with thousands separator and two decimals, no currency (`#,##0.00`), and a
        # percentage without decimals (`#%`). Own entries for the same reason as `moeda_colada` --
        # the vocabulary is closed to prevent ARBITRARY formats, not formats a file must keep.
        "decimal",
        "percentual_inteiro",
        "inteiro",
        "texto",
    }


def test_nome_aba_sanitiza_e_limita_31():
    assert layout.nome_aba("REF 1: NPV/rights [x]") == "REF 1  NPV rights  x"
    assert len(layout.nome_aba("a" * 80)) == 31
    assert layout.nome_aba("   ") == "Dados"
