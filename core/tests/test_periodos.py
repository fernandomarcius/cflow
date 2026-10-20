"""Golden of `cflow_core.periodos.resolver_janela`: half-open window [start, end), with a FIXED
`hoje` (no clock dependency). Week Sunday->Saturday. The client mirror in the web app
(`periodos.ts`) must produce the SAME tuples (client/backend parity).

Reference `hoje` = 2026-06-22 (Monday; Sunday of the week = 2026-06-21)."""

from __future__ import annotations

from datetime import date

import pytest

from cflow_core.periodos import resolver_corte, resolver_janela

HOJE = date(2026, 6, 22)

GOLDEN = {
    "mes_atual": ("2026-06-01", "2026-07-01"),
    "mes_anterior": ("2026-05-01", "2026-06-01"),
    "atual": ("2026-06-01", "2026-07-01"),  # month alias (period back-compat)
    "anterior": ("2026-05-01", "2026-06-01"),
    "semana_atual": ("2026-06-21", "2026-06-28"),
    "semana_anterior": ("2026-06-14", "2026-06-21"),
    "hoje": ("2026-06-22", "2026-06-23"),
    "ontem": ("2026-06-21", "2026-06-22"),
    "ultimos_7_dias": ("2026-06-16", "2026-06-23"),
    "ultimos_30_dias": ("2026-05-24", "2026-06-23"),
    "2026-02": ("2026-02-01", "2026-03-01"),  # specific month
    "2024-12": ("2024-12-01", "2025-01-01"),  # crosses the year
    "2026-06-10..2026-06-20": ("2026-06-10", "2026-06-21"),  # range: inclusive ends
}


@pytest.mark.parametrize("token,esperado", list(GOLDEN.items()))
def test_resolver_janela_golden(token: str, esperado: tuple[str, str]) -> None:
    assert resolver_janela(token, HOJE) == esperado


def test_mes_anterior_vira_o_ano() -> None:
    # January -> December of the previous year.
    assert resolver_janela("mes_anterior", date(2026, 1, 15)) == ("2025-12-01", "2026-01-01")


def test_semana_quando_hoje_e_domingo() -> None:
    # Sunday: the current week starts on the day itself.
    assert resolver_janela("semana_atual", date(2026, 6, 21)) == ("2026-06-21", "2026-06-28")


def test_intervalo_invertido_erro() -> None:
    with pytest.raises(ValueError):
        resolver_janela("2026-06-20..2026-06-10", HOJE)


def test_token_desconhecido_erro() -> None:
    with pytest.raises(ValueError):
        resolver_janela("trimestre", HOJE)


# ---- resolver_corte (as-of / system time) -> inclusive timestamp ----

CORTE_GOLDEN = {
    "hoje": "2026-06-22 23:59:59",
    "ontem": "2026-06-21 23:59:59",
    "2026-05": "2026-05-31 23:59:59",  # clean end of May
    "mes_anterior": "2026-05-31 23:59:59",
    "2024-12": "2024-12-31 23:59:59",
}


@pytest.mark.parametrize("valor,esperado", list(CORTE_GOLDEN.items()))
def test_resolver_corte_tokens(valor: str, esperado: str) -> None:
    assert resolver_corte(valor, HOJE) == esperado


def test_resolver_corte_data_literal_vira_fim_do_dia() -> None:
    assert resolver_corte("2026-05-15") == "2026-05-15 23:59:59"


def test_resolver_corte_timestamp_literal_preservado() -> None:
    assert resolver_corte("2026-05-15 14:30:00") == "2026-05-15 14:30:00"
    assert resolver_corte("2026-05-15T14:30:00") == "2026-05-15 14:30:00"  # 'T' -> space


def test_resolver_corte_invalido_erro() -> None:
    with pytest.raises(ValueError):
        resolver_corte("trimestre", HOJE)
