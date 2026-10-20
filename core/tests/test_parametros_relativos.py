"""Day-dependent value: the same input normalizes differently tomorrow, so it cannot be used to
recognize a run later. This is the `relativos` of `/parametros/normalizados`.

Only `competencia` and `as_of` resolve a token; in a `texto`, "hoje" is just the word. The default
counts when the value comes in blank (it is what normalization uses), and the calendar-derived
parameter inherits.
"""

from __future__ import annotations

import pytest

from cflow_core.periodos import eh_relativo
from cflow_core.pipelines.definicao import parametros_relativos


@pytest.mark.parametrize(
    "valor",
    [
        "atual",
        "anterior",
        "hoje",
        "ontem",
        "mes_atual",
        "mes_anterior",
        "semana_atual",
        "ultimos_7_dias",
        " hoje ",
    ],
)
def test_token_e_relativo(valor: str) -> None:
    assert eh_relativo(valor)


@pytest.mark.parametrize(
    "valor",
    ["2026-08", "2026-08-06", "2026-08-06 12:00:00", "2026-08-01..2026-08-31", "", None, 5],
)
def test_literal_nao_e(valor) -> None:
    assert not eh_relativo(valor)


D = {
    "parametros": [
        {"nome": "competencia", "tipo": "competencia", "default": "atual"},
        {"nome": "data_registro", "tipo": "as_of"},
        {"nome": "rotulo", "tipo": "texto"},
        {
            "nome": "competencia_m1",
            "tipo": "texto",
            "derivacao": {"origem": "competencia", "de": "competencia", "meses": -1},
        },
    ]
}


def test_padrao_relativo_conta_quando_o_valor_vem_em_branco() -> None:
    relativos = parametros_relativos(D, {"competencia": "", "data_registro": "2026-08-06"})
    assert relativos == ["competencia", "competencia_m1"]


def test_literal_informado_vence_o_padrao_relativo() -> None:
    assert parametros_relativos(D, {"competencia": "2026-08", "data_registro": "2026-08-06"}) == []


def test_as_of_relativo_conta_e_texto_nao() -> None:
    valores = {"competencia": "2026-08", "data_registro": "hoje", "rotulo": "hoje"}
    assert parametros_relativos(D, valores) == ["data_registro"]
