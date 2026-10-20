"""CALENDAR-derived parameter: `competencia_m1` = the period minus 1 month, on day 1.

What is proven here, without a database:
- the computation (`periodos.deslocar_competencia`) against a table of cases, including what it
  refuses;
- the computation against an independent reference, for every month from 1900 to 2100;
- the period and the derived parameter use the SAME predicate: text the period accepts never leaves
  the derived parameter without a value (the SQL would bind NULL, which means "return everything");
- validation refuses every way for the derived parameter to end up without a value or with a fixed
  value;
- resolution: raw value, period default, expanded window (re-run/preview), the preview's "all"
  window, equal supplied value, divergent supplied value (400) and the lenient preview.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.periodos import deslocar_competencia, eh_competencia
from cflow_core.pipelines.definicao import (
    normalizar_parametros,
    resolver_derivados_competencia,
    validar,
)

# Each case: input period, expected (m1, m2) or None when the computation must refuse.
CASOS = [
    ("2026-08", ("2026-07-01", "2026-06-01")),
    ("2026-01", ("2025-12-01", "2025-11-01")),
    ("2026-02", ("2026-01-01", "2025-12-01")),
    ("2024-03", ("2024-02-01", "2024-01-01")),
    ("2000-01", ("1999-12-01", "1999-11-01")),
    ("1900-03", ("1900-02-01", "1900-01-01")),
    ("9999-12", ("9999-11-01", "9999-10-01")),
    ("0001-03", ("0001-02-01", "0001-01-01")),
    ("0001-01", None),
    ("0001-02", None),
    ("2026-13", None),
    ("2026-00", None),
    ("2026-8", None),
    ("26-08", None),
    (" 2026-08", None),
    ("2026-08\n", None),
    ("", None),
    ("abc", None),
]


def _anteriores(comp: str) -> tuple[str, str]:
    """The computation under test: the two previous periods."""
    return deslocar_competencia(comp, -1), deslocar_competencia(comp, -2)


# --- the computation -----------------------------------------------------------------------


def test_a_tabela_tem_aceitos_e_recusados() -> None:
    assert len(CASOS) >= 10
    assert any(esperado is None for _, esperado in CASOS)
    assert any(esperado is not None for _, esperado in CASOS)


@pytest.mark.parametrize(("competencia", "esperado"), CASOS, ids=lambda v: repr(v))
def test_a_conta_segue_a_tabela(competencia: str, esperado: tuple[str, str] | None) -> None:
    if esperado is None:
        with pytest.raises(ValueError):
            _anteriores(competencia)
    else:
        assert _anteriores(competencia) == esperado


def _referencia(ano: int, mes: int, meses: int) -> str:
    """Walks month by month through the calendar: another computation, so the function is not
    judged by itself."""
    d = date(ano, mes, 1)
    for _ in range(abs(meses)):
        if meses < 0:
            d = (d - timedelta(days=1)).replace(day=1)
        else:
            d = (d + timedelta(days=32)).replace(day=1)
    return d.isoformat()


def test_todo_mes_de_1900_a_2100_contra_a_referencia() -> None:
    for ano in range(1900, 2101):
        for mes in range(1, 13):
            comp = f"{ano:04d}-{mes:02d}"
            for meses in (-1, -2, -13, 1, 12):
                assert deslocar_competencia(comp, meses) == _referencia(ano, mes, meses), (
                    comp,
                    meses,
                )


def test_tokens_sao_relativos_a_hoje_em_sao_paulo() -> None:
    hoje = date(2026, 1, 15)
    assert deslocar_competencia("atual", -1, hoje) == "2025-12-01"
    assert deslocar_competencia("anterior", -1, hoje) == "2025-11-01"


@pytest.mark.parametrize("valor", ["2026-08\n", " 2026-08", "２０２６-08", "2026-8", "2026-13"])
def test_o_que_a_competencia_recusa_o_derivado_tambem_recusa(valor: str) -> None:
    """A single predicate. `2026-08\\n` and the full-width digit used to pass the period check
    (Python's `$` and `\\d`); now all three sides agree."""
    assert not eh_competencia(valor)
    with pytest.raises(ErroAplicacao):
        normalizar_parametros(
            {"parametros": [{"nome": "competencia", "tipo": "competencia"}]},
            {"competencia": valor},
        )
    with pytest.raises(ValueError):
        deslocar_competencia(valor, -1)


# --- validation ----------------------------------------------------------------------------

OP = {"nome": "operacao_id", "tipo": "texto", "obrigatorio": True}
COMP = {"nome": "competencia", "tipo": "competencia", "obrigatorio": True}
M1 = {
    "nome": "competencia_m1",
    "tipo": "texto",
    "derivacao": {"origem": "competencia", "de": "competencia", "meses": -1},
}
M2 = {**M1, "nome": "competencia_m2", "derivacao": {**M1["derivacao"], "meses": -2}}


def _def(*parametros: dict) -> dict:
    return {
        "nos": [{"id": "q", "tipo": "sql", "sql": "SELECT 1 AS um"}],
        "arestas": [],
        "parametros": list(parametros),
    }


def test_derivado_de_calendario_valido() -> None:
    validar(_def(OP, COMP, M1, M2))
    # a period with a default also works as the source (`default: "atual"`)
    validar(_def({"nome": "competencia", "tipo": "competencia", "default": "atual"}, M1))
    # a derived parameter declared BEFORE its source period is accepted
    validar(_def(M1, COMP))


def _der(**campos) -> dict:
    return {**M1, "derivacao": {"origem": "competencia", **campos}}


@pytest.mark.parametrize(
    ("derivado", "competencia", "trecho"),
    [
        # `trecho` is a language-neutral fragment (a quoted identifier) of the error message.
        (_der(meses=-1), COMP, "'de'"),
        (_der(de="nao_existe", meses=-1), COMP, "nao_existe"),
        (_der(de="operacao_id", meses=-1), COMP, "operacao_id"),
        (M1, {"nome": "competencia", "tipo": "competencia"}, "competencia"),
        (_der(de="competencia", meses=0), COMP, "'meses'"),
        (_der(de="competencia", meses=True), COMP, "'meses'"),
        (_der(de="competencia", meses="-1"), COMP, "'meses'"),
        (_der(de="competencia", meses=121), COMP, "'meses'"),
        (_der(de="competencia"), COMP, "'meses'"),
        ({**M1, "tipo": "inteiro"}, COMP, "competencia_m1"),
        ({**M1, "default": "2026-07-01"}, COMP, "competencia_m1"),
        ({**M1, "segredo": True}, COMP, "competencia_m1"),
    ],
)
def test_a_validacao_recusa(derivado: dict, competencia: dict, trecho: str) -> None:
    with pytest.raises(ErroAplicacao) as exc:
        validar(_def(OP, competencia, derivado))
    mensagens = [d.mensagem for d in exc.value.detalhes]
    # Exactly one refusal per case, and it names the derived parameter.
    assert len(mensagens) == 1, mensagens
    assert "competencia_m1" in mensagens[0] and trecho in mensagens[0], mensagens


# --- resolution ----------------------------------------------------------------------------

D = _def(COMP, M1, M2)


def test_valor_cru() -> None:
    r = resolver_derivados_competencia(D, {"competencia": "2026-08"})
    assert (r["competencia_m1"], r["competencia_m2"]) == ("2026-07-01", "2026-06-01")


def test_padrao_da_competencia() -> None:
    d = _def({"nome": "competencia", "tipo": "competencia", "default": "2026-03"}, M1)
    assert resolver_derivados_competencia(d, {})["competencia_m1"] == "2026-02-01"


def test_janela_expandida_de_um_mes__a_reexecucao_repassa_o_guardado() -> None:
    guardado = {
        "competencia_inicio": "2026-08-01",
        "competencia_fim": "2026-09-01",
        "competencia_m1": "2026-07-01",
    }
    assert resolver_derivados_competencia(D, guardado)["competencia_m1"] == "2026-07-01"
    # a run stored before the derived parameter existed, without m1: computed the same
    sem = {k: v for k, v in guardado.items() if k != "competencia_m1"}
    assert resolver_derivados_competencia(D, sem)["competencia_m2"] == "2026-06-01"


def test_janela_tudo_da_previa_nao_e_competencia() -> None:
    previa = {
        "competencia_inicio": "0001-01-01",
        "competencia_fim": "9999-12-31",
        "competencia_m1": None,
    }
    assert resolver_derivados_competencia(D, previa, estrito=False)["competencia_m1"] is None


def test_informado_igual_passa_e_em_branco_e_calculado() -> None:
    r = resolver_derivados_competencia(
        D, {"competencia": "2026-01", "competencia_m1": "2025-12-01", "competencia_m2": ""}
    )
    assert (r["competencia_m1"], r["competencia_m2"]) == ("2025-12-01", "2025-11-01")


def test_informado_divergente_e_recusa_alta_nomeando_os_dois_valores() -> None:
    with pytest.raises(ErroAplicacao) as exc:
        resolver_derivados_competencia(
            D, {"competencia": "2026-08", "competencia_m1": "2026-08-01"}
        )
    assert exc.value.codigo == "parametro_derivado_divergente"
    assert exc.value.status == 400
    mensagem = exc.value.detalhes[0].mensagem
    assert "2026-08-01" in mensagem and "2026-07-01" in mensagem


def test_a_previa_e_leniente__o_valor_de_preview_vence() -> None:
    r = resolver_derivados_competencia(
        D, {"competencia": "2026-08", "competencia_m1": "2020-01-01"}, estrito=False
    )
    assert r["competencia_m1"] == "2020-01-01"


def test_competencia_invalida_nao_vira_derivado__quem_acusa_e_a_normalizacao() -> None:
    r = resolver_derivados_competencia(D, {"competencia": "2026-13"})
    assert "competencia_m1" not in r
    with pytest.raises(ErroAplicacao):
        normalizar_parametros(D, r)


def test_competencia_aceita_com_deslocamento_fora_do_calendario_e_recusa_alta() -> None:
    """`0001-01` is a period that normalization ACCEPTS; -1 month falls in year 0. With no value, the
    derived parameter would become NULL in the SQL ("return everything"). It is a refusal, naming
    the derived parameter."""
    normalizar_parametros(_def(COMP), {"competencia": "0001-01"})  # the period passes
    with pytest.raises(ErroAplicacao) as exc:
        resolver_derivados_competencia(D, {"competencia": "0001-01"})
    assert exc.value.codigo == "parametro_derivado_invalido"
    assert "competencia_m1" in exc.value.detalhes[0].mensagem


def test_9999_12_a_conta_faz_e_a_competencia_recusa_pelo_nome_dela() -> None:
    """The computation does not go through the window (which overflows at year 10000); it is the
    `9999-12` period that normalization refuses, and the refusal names the right parameter, not
    the derived one."""
    assert deslocar_competencia("9999-12", -1) == "9999-11-01"
    r = resolver_derivados_competencia(D, {"competencia": "9999-12"})
    assert r["competencia_m1"] == "9999-11-01"
    with pytest.raises(ErroAplicacao) as exc:
        normalizar_parametros(D, r)
    assert any("'competencia'" in d.mensagem for d in exc.value.detalhes)


def test_derivado_de_mapa_nao_e_tocado() -> None:
    d = _def(
        {"nome": "op", "tipo": "texto"},
        {
            "nome": "sistema",
            "tipo": "texto",
            "derivacao": {"de": "op", "campo_id": "a", "campo_valor": "b", "mapa": "m"},
        },
    )
    assert resolver_derivados_competencia(d, {"op": "1"}) == {"op": "1"}


def test_a_normalizacao_guarda_o_derivado_calculado() -> None:
    n = normalizar_parametros(D, resolver_derivados_competencia(D, {"competencia": "2026-08"}))
    assert n == {
        "competencia_inicio": "2026-08-01",
        "competencia_fim": "2026-09-01",
        "competencia_m1": "2026-07-01",
        "competencia_m2": "2026-06-01",
    }


async def test_o_resolvedor_da_execucao_calcula_o_de_calendario() -> None:
    """`resolver_derivados` is what the run, the preview and the daily cache call."""
    from cflow_core.pipelines.derivacao import resolver_derivados

    r = await resolver_derivados(D, {"competencia": "2026-08"})
    assert r["competencia_m2"] == "2026-06-01"
    with pytest.raises(ErroAplicacao):
        await resolver_derivados(D, {"competencia": "2026-08", "competencia_m2": "2026-01-01"})
    leniente = await resolver_derivados(
        D, {"competencia": "2026-08", "competencia_m2": "2026-01-01"}, estrito=False
    )
    assert leniente["competencia_m2"] == "2026-01-01"
