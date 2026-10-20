"""Conversion profiles per family (silver).

The case this file exists to lock is the fixed-width one: `PRICE = '0000000003227'` is 32.27, and a
direct `TRY_CAST` returns 3227 **without failing**. Data a hundred times wrong that looks right is
the worst outcome for a financial consumer: worse than an error, because nobody will investigate.
"""

from __future__ import annotations

from decimal import Decimal

import duckdb
import pytest

from cflow_core.ingestao import perfis as P


def _valor(coluna: str, expr: str, texto: str | None):
    """Run the expression against a table holding the REAL column.

    The name matters: the expression quotes it, and source names carry spaces, dots and slashes."""
    con = duckdb.connect()
    try:
        alvo = '"' + coluna.replace('"', '""') + '"'
        con.execute(f"CREATE TABLE t AS SELECT ? AS {alvo}", [texto])
        return con.execute(f"SELECT {expr} FROM t").fetchone()[0]
    finally:
        con.close()


@pytest.mark.parametrize(
    ("dataset", "familia"),
    [
        ("monthly_registry", "iso"),
        ("dmy_rates_daily", "dmy"),
        ("api_fx_rates", "api"),
        ("api_expectations_monthly", "api"),
        ("comma_bond_quotes", "comma"),
        ("fixed_quotes_daily", "fixed"),
    ],
)
def test_familia_por_dataset(dataset: str, familia: str) -> None:
    assert P.familia_de(dataset) == familia


def test_fixed_preco_tem_decimal_implicito() -> None:
    """`0000000003227` is 32.27, not 3,227. Without the scale, every price comes out 100x larger."""
    perfil = P.perfil_de("fixed_quotes_daily")
    expr = P.expressao_numero("LASTPRICE", perfil)
    assert _valor("LASTPRICE", expr, "0000000003227") == pytest.approx(32.27)


def test_fixed_volume_tambem_escala() -> None:
    """`000000030219856600` = 302,198,566.00."""
    perfil = P.perfil_de("fixed_quotes_daily")
    volume = _valor("TOTVOL", P.expressao_numero("TOTVOL", perfil), "000000030219856600")
    assert volume == pytest.approx(302198566.00)


def test_fixed_contagem_NAO_escala() -> None:
    """`TOTTRADES` is a trade count: a real integer. Dividing by 100 would be wrong in the other
    direction, and just as silent."""
    perfil = P.perfil_de("fixed_quotes_daily")
    assert _valor("TOTTRADES", P.expressao_numero("TOTTRADES", perfil), "00350") == pytest.approx(
        350
    )


def test_comma_usa_virgula_decimal() -> None:
    perfil = P.perfil_de("comma_bond_quotes")
    taxa = _valor("Indicative Rate", P.expressao_numero("Indicative Rate", perfil), "0,693")
    pu = _valor("PU", P.expressao_numero("PU", perfil), "1076,108726")
    assert taxa == Decimal("0.693")
    assert pu == Decimal("1076.108726")


def test_iso_usa_ponto_decimal() -> None:
    perfil = P.perfil_de("monthly_registry")
    v = _valor("TOTAL_AMOUNT", P.expressao_numero("TOTAL_AMOUNT", perfil), "1095646.22")
    assert v == Decimal("1095646.22")


def test_valor_monetario_sai_DECIMAL_nao_float() -> None:
    """Money in DOUBLE accumulates binary residue: `0.1+0.2` becomes `0.30000000000000004`, which
    resurfaces as a one-cent difference in reconciliation. The cast is DECIMAL(28,10)."""
    v = _valor("AMT", P.expressao_numero("AMT", P.PERFIS["iso"]), "0.1")
    assert isinstance(v, Decimal)


def test_datas_por_familia() -> None:
    """Each family writes dates its own way; the profile knows which."""
    import datetime

    iso = _valor("DT", P.expressao_data("DT", P.PERFIS["iso"]), "2026-01-01")
    dmy = _valor("data", P.expressao_data("data", P.PERFIS["dmy"]), "01/12/2019")
    assert iso == datetime.date(2026, 1, 1)
    assert dmy == datetime.date(2019, 12, 1)
    assert _valor(
        "TRADE_DATE", P.expressao_data("TRADE_DATE", P.PERFIS["fixed"]), "20260814"
    ) == datetime.date(2026, 8, 14)


def test_data_em_formato_errado_vira_nulo_nao_data_errada() -> None:
    """`01/12/2019` read as ISO must not become December 1st by accident: it becomes NULL, which
    the loss guard detects."""
    assert _valor("d", P.expressao_data("d", P.PERFIS["iso"]), "01/12/2019") is None


def test_identificador_nao_vira_numero() -> None:
    """A tax ID with leading zeros stops being the same ID; a trading code stops matching the
    registry. They convert 100 %, which is exactly why they need an explicit veto."""
    iso = P.PERFIS["iso"]
    assert P.pode_ser_numero("TOTAL_AMOUNT", iso) is True
    assert P.pode_ser_numero("Tax_Id_Issuer", iso) is False
    assert P.pode_ser_numero("Certificate_Code", iso) is False
    assert P.pode_ser_numero("TICKER", P.PERFIS["fixed"]) is False


def test_valor_nao_convertivel_vira_nulo() -> None:
    """`TRY_CAST` does not blow up, it yields NULL. The loss guard is what turns that into a
    failure."""
    assert _valor("x", P.expressao_numero("x", P.PERFIS["iso"]), "not-a-number") is None


def test_coluna_com_aspas_no_nome_e_quotada() -> None:
    """Column names come from the source: `Due Dt./  Amt.` has a dot, a slash and spaces."""
    expr = P.expressao_numero('Rate "special"', P.PERFIS["iso"])
    assert '"Rate ""special"""' in expr
