"""Next-trigger computation (`agendador.proxima_em`): pure, no database. Times in
America/Sao_Paulo (UTC-3 in June 2026, no daylight saving). Fixed `depois` = 2026-06-22 12:00Z
(= 09:00 in SP, a Monday)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from cflow_core.execucao.agendador import proxima_em

U = UTC
DEPOIS = datetime(2026, 6, 22, 12, 0, tzinfo=U)  # 09:00 SP, Monday


@pytest.mark.parametrize(
    "agenda,esperado",
    [
        ({"tipo": "horaria", "a_cada_horas": 2}, datetime(2026, 6, 22, 14, 0, tzinfo=U)),
        (
            {"tipo": "diaria", "horario": "06:00"},
            datetime(2026, 6, 23, 9, 0, tzinfo=U),
        ),  # already passed today -> tomorrow
        (
            {"tipo": "diaria", "horario": "18:00"},
            datetime(2026, 6, 22, 21, 0, tzinfo=U),
        ),  # still today
        (
            {"tipo": "semanal", "dias_semana": [3], "horario": "06:00"},
            datetime(2026, 6, 24, 9, 0, tzinfo=U),
        ),  # Wednesday
        (
            {"tipo": "mensal", "dia_mes": 1, "horario": "06:00"},
            datetime(2026, 7, 1, 9, 0, tzinfo=U),
        ),  # day 1 already passed -> next month
    ],
)
def test_proxima_em(agenda: dict, esperado: datetime) -> None:
    assert proxima_em(agenda, depois=DEPOIS) == esperado


def test_vigencia_inicio_no_futuro() -> None:
    # The first run cannot be before the start of the validity period.
    assert proxima_em(
        {"tipo": "diaria", "horario": "06:00"}, vigencia_inicio=date(2026, 7, 10), depois=DEPOIS
    ) == datetime(2026, 7, 10, 9, 0, tzinfo=U)


def test_mensal_dia_31_encaixa_em_fevereiro() -> None:
    # dia_mes=31 in February -> last day of the month (clamp), no overflow.
    fev = datetime(2026, 2, 10, 12, 0, tzinfo=U)
    prox = proxima_em({"tipo": "mensal", "dia_mes": 31, "horario": "06:00"}, depois=fev)
    assert prox == datetime(2026, 2, 28, 9, 0, tzinfo=U)
