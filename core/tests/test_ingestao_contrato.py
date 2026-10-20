"""Declarative external-source contract: validation, slicing and URL resolution.

Pure logic, no database and no network. The contract is the grammar that keeps a badly registered
source from becoming an execution; what this file proves is that it rejects what it must reject.
"""

from __future__ import annotations

from datetime import date

import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.ingestao import contrato as C
from cflow_core.ingestao.catalogo import CATALOGO


def _base(**over) -> dict:
    base = {
        "classe": "http_file",
        "dataset": "fonte_teste",
        "cadencia": "mensal",
        "janela": {"tipo": "mensal", "inicio": "2026-01"},
        "acesso": {"url_padrao": "https://example.com/{ano}{mes}.zip"},
        "formato": {"tipo": "csv", "encoding": "latin-1", "delimitador": ";"},
    }
    base.update(over)
    return base


def _detalhes(exc: ErroAplicacao) -> str:
    return " | ".join(d.mensagem for d in exc.detalhes)


def test_catalogo_inteiro_e_valido() -> None:
    for ds, c in CATALOGO.items():
        C.validar(c)  # does not raise
        assert c["dataset"] == ds


def test_encoding_obrigatorio_em_csv() -> None:
    """Without a declared encoding accents are silently corrupted: a registration error, not a default."""
    c = _base(formato={"tipo": "csv", "delimitador": ";"})
    with pytest.raises(ErroAplicacao) as e:
        C.validar(c)
    assert e.value.status == 422
    assert "encoding" in _detalhes(e.value)


def test_encoding_desconhecido_recusado() -> None:
    with pytest.raises(ErroAplicacao) as e:
        C.validar(_base(formato={"tipo": "csv", "encoding": "latim-um"}))
    assert "unknown" in _detalhes(e.value)


def test_placeholder_desconhecido_recusado() -> None:
    """A URL with `{serie}` and no declared variable would return 404 every day without saying why."""
    c = _base(acesso={"url_padrao": "https://example.com/serie/{serie}/dados"})
    with pytest.raises(ErroAplicacao) as e:
        C.validar(c)
    assert "{serie}" in _detalhes(e.value)


def test_placeholder_declarado_em_variaveis_passa() -> None:
    C.validar(
        _base(
            classe="rest_api",
            acesso={
                "url_padrao": "https://example.com/serie/{serie}/dados",
                "variaveis": {"serie": "11"},
            },
            formato={"tipo": "json"},
        )
    )


def test_url_precisa_ser_https() -> None:
    with pytest.raises(ErroAplicacao) as e:
        C.validar(_base(acesso={"url_padrao": "http://example.com/a.zip"}))
    assert "https" in _detalhes(e.value)


def test_periodo_exige_passo_max_dias() -> None:
    with pytest.raises(ErroAplicacao) as e:
        C.validar(_base(janela={"tipo": "periodo", "inicio": "2020-01-01"}))
    assert "passo_max_dias" in _detalhes(e.value)


def test_dataset_precisa_ser_slug() -> None:
    with pytest.raises(ErroAplicacao) as e:
        C.validar(_base(dataset="Fonte Teste!"))
    assert "slug" in _detalhes(e.value)


def test_varios_problemas_de_uma_vez() -> None:
    """One error per round-trip is what makes registering a source painful."""
    with pytest.raises(ErroAplicacao) as e:
        C.validar({"classe": "xpto", "dataset": "X", "janela": {}, "formato": {}, "acesso": {}})
    assert len(e.value.detalhes) >= 4


def test_largura_fixa_exige_campos() -> None:
    with pytest.raises(ErroAplicacao) as e:
        C.validar(_base(formato={"tipo": "largura_fixa", "encoding": "cp1252"}))
    assert "campos" in _detalhes(e.value)


# --- Slicing -------------------------------------------------------------------------------


def test_fatia_mensal() -> None:
    f = C.fatias(_base(), date(2026, 6, 10), date(2026, 8, 3))
    assert [x.chave for x in f] == ["2026-06", "2026-07", "2026-08"]
    assert f[0].inicio == date(2026, 6, 1) and f[0].fim == date(2026, 6, 30)


def test_fatia_periodo_respeita_passo_de_dez_anos() -> None:
    """Some origins refuse windows longer than 10 years: slicing is what makes the backfill fit."""
    c = _base(janela={"tipo": "periodo", "inicio": "2000-01-01", "passo_max_dias": 3650})
    f = C.fatias(c, date(2000, 1, 1), date(2026, 8, 15))
    assert len(f) == 3
    # `passo_max_dias` counts days INCLUSIVELY: 3650 days starting on 01/01 end on 12/28 nine years on.
    assert all((x.fim - x.inicio).days + 1 == 3650 for x in f)
    assert f[0].chave == "2000-01-01..2009-12-28"


def test_fatia_periodo_tem_chave_ESTAVEL_no_tempo() -> None:
    """The key of the current slice must not move with the calendar.

    Truncating the last slice at "today" would make the key `..2026-08-15`, `..2026-08-16`, and a
    new key is a new BATCH: idempotency would stop holding exactly on the most reloaded slice, and
    a year of reloads would leave twelve overlapping windows that anyone reading the whole
    dataset would add up."""
    c = _base(janela={"tipo": "periodo", "inicio": "2010-01-01", "passo_max_dias": 3650})
    ontem = C.fatias(c, date(2010, 1, 1), date(2026, 8, 15))[-1].chave
    hoje = C.fatias(c, date(2010, 1, 1), date(2026, 8, 16))[-1].chave
    daqui_um_mes = C.fatias(c, date(2010, 1, 1), date(2026, 9, 16))[-1].chave
    assert ontem == hoje == daqui_um_mes == "2019-12-30..2029-12-26"


def test_fatia_periodo_alinha_na_grade_do_contrato() -> None:
    """The requested window SELECTS slices of the grid; it does not redefine its boundaries.
    Without this, a one-month backfill would create its own slice, overlapping the grid's."""
    c = _base(janela={"tipo": "periodo", "inicio": "2010-01-01", "passo_max_dias": 3650})
    f = C.fatias(c, date(2026, 8, 1), date(2026, 8, 15))
    assert [x.chave for x in f] == ["2019-12-30..2029-12-26"]


def test_piso_da_janela_protege_contra_backfill_antigo() -> None:
    c = _base(janela={"tipo": "anual", "inicio": "2024"})
    f = C.fatias(c, date(1990, 1, 1), date(2026, 1, 1))
    assert [x.chave for x in f] == ["2024", "2025", "2026"]


def test_atraso_dias_ignora_a_fatia_ainda_nao_publicada() -> None:
    c = _base(janela={"tipo": "diaria", "inicio": "2026-08-01", "atraso_dias": 3})
    f = C.fatias(c, date(2026, 8, 5), date(2026, 8, 12))
    assert [x.chave for x in f] == [
        "2026-08-05",
        "2026-08-06",
        "2026-08-07",
        "2026-08-08",
        "2026-08-09",
    ]
    # Window ENTIRELY inside the lag: nothing to do (not an error; it is simply not published yet).
    assert C.fatias(c, date(2026, 8, 10), date(2026, 8, 12)) == []


def test_janela_invertida_devolve_vazio() -> None:
    assert C.fatias(_base(), date(2026, 8, 1), date(2026, 7, 1)) == []


# --- URL / parameter resolution ------------------------------------------------------------


def test_resolver_url_do_catalogo() -> None:
    f = C.Fatia(chave="2026", inicio=date(2026, 1, 1), fim=date(2026, 12, 31))
    url = C.resolver(CATALOGO["registry_monthly"]["acesso"]["url_padrao"], f, None)
    assert url == "https://example.com/files/registry_2026.zip"


def test_resolver_formata_data_no_padrao_do_odata() -> None:
    f = C.Fatia(chave="2026-07", inicio=date(2026, 7, 1), fim=date(2026, 7, 31))
    assert C.resolver("'{inicio:%m-%d-%Y}'", f, None) == "'07-01-2026'"


def test_resolver_recusa_placeholder_fora_do_contrato() -> None:
    f = C.Fatia(chave="2026-07", inicio=date(2026, 7, 1), fim=date(2026, 7, 31))
    with pytest.raises(ErroAplicacao) as e:
        C.resolver("https://x/{inexistente}", f, None)
    assert e.value.codigo == "placeholder_desconhecido"


def test_hash_do_contrato_e_estavel_e_sensivel() -> None:
    a = _base()
    b = _base()
    b["formato"] = {**b["formato"], "encoding": "cp1252"}
    assert C.hash_contrato(a) == C.hash_contrato(_base())
    assert C.hash_contrato(a) != C.hash_contrato(b)


# --- Single overwritten file (origin without history) --------------------------------------


def _fonte_url_fixa(**over) -> dict:
    c = _base(
        janela={"tipo": "diaria", "inicio": "2026-01-01"},
        acesso={"url_padrao": "https://example.com/files/current.csv"},
    )
    c.update(over)
    return c


def test_url_fixa_so_gera_a_fatia_mais_recente() -> None:
    """A fixed-URL file is overwritten by the origin: there is no history. Generating one slice
    per day would download TODAY's file three times and label it with three dates: fake history,
    which is worse than none because it looks like data."""
    f = C.fatias(_fonte_url_fixa(), date(2026, 8, 12), date(2026, 8, 15))
    assert [x.chave for x in f] == ["2026-08-15"]


def test_url_com_data_gera_a_janela_toda() -> None:
    c = _base(
        janela={"tipo": "diaria", "inicio": "2026-01-01"},
        acesso={"url_padrao": "https://example.com/files/daily_{ano_curto}{mes}{dia}.txt"},
    )
    assert len(C.fatias(c, date(2026, 8, 12), date(2026, 8, 14))) == 3


def test_janela_por_parametro_de_query_conta_como_dependente() -> None:
    """Some APIs do not put the date in the URL: it goes in query parameters (`from`/`to`)."""
    assert C.depende_da_fatia(CATALOGO["rates_daily"]) is True


def test_odata_com_filtro_de_periodo_conta_como_dependente() -> None:
    assert C.depende_da_fatia(CATALOGO["fx_quotes"]) is True


def test_catalogo_marca_as_fontes_de_arquivo_unico() -> None:
    """Pins the list: if a NEW source falls in here by accident, the test warns."""
    unicas = {ds for ds, c in CATALOGO.items() if not C.depende_da_fatia(c)}
    assert unicas == {"reports_portal"}


def test_odata_marca_codificacao_estrita() -> None:
    """The OData seed contract must come out with strict encoding turned on."""
    import asyncio
    from datetime import date as _d

    from cflow_core.ingestao.conectores import get_conector

    c = CATALOGO["fx_quotes"]
    f = C.fatias(c, _d(2026, 7, 1), _d(2026, 7, 31))[0]
    req = asyncio.run(get_conector("odata").descobrir(c, f))[0]
    assert req.codificar_estrito is True


def test_tolerancia_a_linha_invalida_e_declarada_e_booleana() -> None:
    """Tolerance is DATA of that source's contract, not a global flag: turning it on for everyone
    would trade a visible failure for a silent drop."""
    C.validar(_base(formato={**_base()["formato"], "tolerar_linhas_invalidas": True}))
    with pytest.raises(ErroAplicacao) as e:
        C.validar(_base(formato={**_base()["formato"], "tolerar_linhas_invalidas": "sim"}))
    assert "tolerar_linhas_invalidas" in str(e.value.detalhes)


def test_tolerancia_so_vale_em_csv() -> None:
    """`largura_fixa` and `json` have no "row with an extra field": accepting the flag there would
    promise a behaviour the parser does not have."""
    with pytest.raises(ErroAplicacao):
        C.validar(
            _base(
                formato={
                    "tipo": "largura_fixa",
                    "encoding": "cp1252",
                    "campos": [{"nome": "a", "inicio": 1, "tamanho": 2}],
                    "tolerar_linhas_invalidas": True,
                }
            )
        )


def test_so_uma_fonte_tolera_linha_invalida() -> None:
    """Tolerance must not leak to the other tables: everywhere else, an unexpected row remains the
    signal that the origin changed its layout."""
    com = sorted(
        ds for ds, c in CATALOGO.items() if c.get("formato", {}).get("tolerar_linhas_invalidas")
    )
    assert com == ["registry_monthly"]


def _serie_mensal() -> dict:
    """The example daily series, turned into a monthly one aligned to month end."""
    c = dict(CATALOGO["rates_daily"])
    c["janela"] = {**c["janela"], "alinhar_a": "mes"}
    return c


def test_serie_mensal_corta_a_fatia_no_fim_do_mes() -> None:
    """A duplicate that only a measurement on published data revealed.

    A monthly series dates its point on day 1. With the 3,650-day grid cutting on 12/29, the
    December point entered the slice ending on 12/29 **and** the one starting on 12/30: two
    distinct `chave_lote`. Neither `DISTINCT ON (chave_lote)` nor the unique index
    `(fonte, chave, sha)` protects across different keys, so the series ended up with more rows
    than dates."""
    serie = _serie_mensal()
    chaves = [f.chave for f in C.fatias(serie, date(2010, 1, 1), date(2026, 8, 16))]
    assert chaves == ["2010-01-01..2019-12-31", "2020-01-01..2029-12-31"]
    # what matters is not the key format: it is that no day is covered by two slices
    fs = C.fatias(serie, date(2010, 1, 1), date(2026, 8, 16))
    for anterior, seguinte in zip(fs, fs[1:], strict=False):
        assert anterior.fim < seguinte.inicio, "slices must not overlap"


def test_serie_diaria_nao_e_alinhada() -> None:
    """Daily series escaped by calendar accident (12/29 and 12/30 are distinct days), not by
    control. The alignment is declared, so they keep the usual grid."""
    chaves = [
        f.chave for f in C.fatias(CATALOGO["rates_daily"], date(2010, 1, 1), date(2026, 8, 16))
    ]
    assert chaves == ["2010-01-01..2019-12-29", "2019-12-30..2029-12-26"]


def test_alinhamento_ao_mes_vale_so_para_quem_declara() -> None:
    """`cadencia` is NOT a signal: it is the execution frequency (monthly for every source), not
    the grain of the series. That is why the marker is explicit in the window."""
    base = _base(
        cadencia="mensal",
        janela={"tipo": "periodo", "inicio": "2020-01-01", "passo_max_dias": 100},
    )
    sem = [f.fim for f in C.fatias(base, date(2020, 1, 1), date(2020, 12, 31))]
    assert sem[0] == date(2020, 4, 9), "without the marker, the grid is not aligned"
