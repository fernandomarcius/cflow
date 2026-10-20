"""The declared grain of the source: what ONE ROW of the table it produces is.

Context: no source declared its grain. The only mechanism was the boolean
`janela.fatia_e_competencia`, which answers another question ("is the slice the competence
date?"). Without a grain, duplicated rows went through with no validation able to see them, and
there was no way to tell "duplicate" from "grain too wide".

Two decisions these tests pin down:

1. **`grao` is a SIBLING of `janela`, not a field inside it.** `janela` says how the run is
   sliced; `grao` says what a row is. Mixing them is the cadence x grain confusion.
2. **Absence is accepted, but it is not silence.** `validar()` does not require `grao`:
   requiring it would break every `PUT` on already registered sources at once. Absence is
   NORMALISED to `desconhecido`, the state about which noise is made. The default is never "the
   keys are all the columns": that would make every table trivially unique and hide exactly the
   tables with identical rows.
"""

from __future__ import annotations

import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.ingestao import contrato as C


def _contrato(grao=None, **over) -> dict:
    """Minimal VALID contract, so the only possible problem is the `grao` under test."""
    c: dict = {
        "classe": "http_file",
        "dataset": "test_source",
        "cadencia": "mensal",
        "janela": {"tipo": "anual", "inicio": "2020"},
        "acesso": {"url_padrao": "https://example.com/data_{ano}.csv"},
        "formato": {"tipo": "csv", "encoding": "latin-1"},
        "schema_esperado": ["Código", "Taxa Indicativa", "PU", "Data Referência"],
    }
    c.update(over)
    if grao is not None:
        c["grao"] = grao
    return c


def _problemas(contrato: dict) -> list[str]:
    """The 422 details, or an empty list if the contract passed."""
    try:
        C.validar(contrato)
    except ErroAplicacao as e:
        return [d.mensagem or "" for d in (e.detalhes or [])]
    return []


# --- absence: accepted, normalised, and never "keys = everything" -----------------------------


def test_ausencia_e_aceita_e_vira_desconhecido():
    assert _problemas(_contrato()) == []
    g = C.grao(_contrato())
    assert g.estado == "desconhecido"
    assert g.desconhecido is True
    assert g.declarado is False


def test_ausencia_nao_inventa_chaves():
    """`keys = all columns` would make any table trivially unique: it is the default that would
    hide exactly the case that motivated the field."""
    assert C.grao(_contrato()).chaves == ()
    assert C.grao({}).chaves == ()


def test_leitura_defensiva_nao_inventa_grao_plausivel():
    """A validated contract never arrives with a state outside the enum; if one does (old data in
    the database), the read says "I don't know" instead of accepting the odd value as a declaration."""
    assert C.grao({"grao": {"estado": "quase", "chaves": ["a"]}}).estado == "desconhecido"
    assert C.grao({"grao": "declarado"}).estado == "desconhecido"


# --- state ----------------------------------------------------------------------------------


def test_estado_fora_do_enum_e_erro():
    p = _problemas(_contrato({"estado": "provavel", "chaves": ["codigo"]}))
    assert any("grao.estado" in m for m in p)


def test_grao_que_nao_e_objeto_e_erro():
    assert any("'grao'" in m for m in _problemas(_contrato(["codigo"])))


# --- declared -------------------------------------------------------------------------------


def test_declarado_caminho_feliz():
    c = _contrato({"estado": "declarado", "chaves": ["codigo", "data_referencia"], "motivo": None})
    assert _problemas(c) == []
    g = C.grao(c)
    assert g.declarado is True
    assert g.chaves == ("codigo", "data_referencia")


def test_declarado_sem_chaves_e_erro():
    """ "Declared" with an empty list is absence disguised as a decision."""
    assert any("grao.chaves" in m for m in _problemas(_contrato({"estado": "declarado"})))
    p = _problemas(_contrato({"estado": "declarado", "chaves": []}))
    assert any("cannot be empty" in m for m in p)


def test_chave_precisa_estar_no_schema_esperado():
    """A typo caught at REGISTRATION, not months later in a count that does not add up."""
    p = _problemas(_contrato({"estado": "declarado", "chaves": ["codgio"]}))
    assert any("'codgio'" in m and "schema_esperado" in m for m in p)


def test_chave_e_o_nome_NORMALIZADO_da_coluna_da_origem():
    """The silver renames (`Código` -> `codigo`, `Taxa Indicativa` -> `taxa_indicativa`); the key
    speaks the silver's language, which is what will check uniqueness."""
    assert _problemas(_contrato({"estado": "declarado", "chaves": ["taxa_indicativa"]})) == []
    p = _problemas(_contrato({"estado": "declarado", "chaves": ["Código"]}))
    assert any("Código" in m for m in p)


def test_chave_de_duas_letras_e_valida():
    """`pu` is a real column in some sources. The `dataset` slug requires 3 characters; the column
    one must not inherit that rule."""
    assert _problemas(_contrato({"estado": "declarado", "chaves": ["pu"]})) == []


def test_chave_repetida_e_erro():
    c = _contrato({"estado": "declarado", "chaves": ["codigo", "codigo"]})
    assert any("repeated" in m for m in _problemas(c))


def test_sem_schema_esperado_nao_ha_o_que_conferir():
    """Some tables do not declare `schema_esperado` (e.g. a zip with several tables of distinct
    grains). There the key is checked against the data, not against the contract: validating
    against a schema that does not exist would reject a legitimate declaration."""
    c = _contrato({"estado": "declarado", "chaves": ["entity_id"]}, schema_esperado=None)
    del c["schema_esperado"]
    assert _problemas(c) == []


# --- tie-break: resolves a tie WITHIN the key, never silently -------------------------------


def test_desempate_caminho_feliz():
    c = _contrato(
        {
            "estado": "declarado",
            "chaves": ["codigo", "data_referencia"],
            "desempate": {
                "colunas": [{"coluna": "pu", "ordem": "desc"}],
                "motivo": "the origin resubmits the same record in a later delivery",
            },
        }
    )
    assert _problemas(c) == []
    g = C.grao(c)
    assert g.desempate == (("pu", "desc"),)
    assert g.desempate_motivo


def test_desempate_sem_motivo_e_erro():
    """Same requirement as `sem_chave`: dropping a row without saying why is the silent drop the
    whole grain exists to prevent."""
    c = _contrato(
        {
            "estado": "declarado",
            "chaves": ["codigo", "data_referencia"],
            "desempate": {"colunas": [{"coluna": "pu", "ordem": "desc"}]},
        }
    )
    assert any("desempate.motivo" in m for m in _problemas(c))


def test_desempate_ordem_invalida_e_erro():
    c = _contrato(
        {
            "estado": "declarado",
            "chaves": ["codigo", "data_referencia"],
            "desempate": {
                "colunas": [{"coluna": "pu", "ordem": "ascending"}],
                "motivo": "x",
            },
        }
    )
    assert any("'asc' or 'desc'" in m for m in _problemas(c))


def test_desempate_colunas_vazia_e_erro():
    c = _contrato(
        {
            "estado": "declarado",
            "chaves": ["codigo", "data_referencia"],
            "desempate": {"colunas": [], "motivo": "x"},
        }
    )
    assert any("cannot be empty" in m for m in _problemas(c))


def test_desempate_coluna_repetida_e_erro():
    c = _contrato(
        {
            "estado": "declarado",
            "chaves": ["codigo", "data_referencia"],
            "desempate": {
                "colunas": [{"coluna": "pu", "ordem": "desc"}, {"coluna": "pu", "ordem": "asc"}],
                "motivo": "x",
            },
        }
    )
    assert any("repeated" in m for m in _problemas(c))


def test_desempate_nao_pode_repetir_coluna_da_chave():
    """A column that already identifies the row does not break ties: it is the identity, not the
    criterion for choosing between copies."""
    c = _contrato(
        {
            "estado": "declarado",
            "chaves": ["codigo", "data_referencia"],
            "desempate": {
                "colunas": [{"coluna": "codigo", "ordem": "desc"}],
                "motivo": "x",
            },
        }
    )
    assert any("cannot repeat 'grao.chaves'" in m for m in _problemas(c))


def test_desempate_coluna_precisa_estar_no_schema_esperado():
    c = _contrato(
        {
            "estado": "declarado",
            "chaves": ["codigo", "data_referencia"],
            "desempate": {
                "colunas": [{"coluna": "column_that_does_not_exist", "ordem": "desc"}],
                "motivo": "x",
            },
        }
    )
    p = _problemas(c)
    assert any("column_that_does_not_exist" in m and "schema_esperado" in m for m in p)


def test_desempate_so_vale_com_estado_declarado():
    c = _contrato(
        {
            "estado": "sem_chave",
            "chaves": [],
            "motivo": "aggregate without identity",
            "desempate": {"colunas": [{"coluna": "pu", "ordem": "desc"}], "motivo": "x"},
        }
    )
    assert any("desempate' only applies with estado='declarado'" in m for m in _problemas(c))


def test_desempate_ausente_nao_e_problema():
    c = _contrato({"estado": "declarado", "chaves": ["codigo"]})
    assert _problemas(c) == []
    assert C.grao(c).desempate == ()


# --- competence: a provenance key, and it must exist in the bronze -----------------------------


def test_competencia_vale_quando_a_fatia_a_projeta():
    c = _contrato(
        {"estado": "declarado", "chaves": ["codigo", "competencia"]},
        janela={"tipo": "diaria", "inicio": "2026-02-23", "fatia_e_competencia": True},
    )
    assert _problemas(c) == []


def test_competencia_sem_fatia_e_competencia_e_erro():
    """Without the flag the column does not even exist in the bronze read: the key would point at
    nothing and the uniqueness check would always pass, vacuously."""
    c = _contrato({"estado": "declarado", "chaves": ["codigo", "competencia"]})
    p = _problemas(c)
    assert any("fatia_e_competencia" in m for m in p)


def test_ingestion_date_e_proveniencia_aceita():
    """`ingestion_date` comes from the hive partitioning, not from `schema_esperado`, but it is a
    real column of the bronze read."""
    c = _contrato({"estado": "declarado", "chaves": ["codigo", "ingestion_date"]})
    assert _problemas(c) == []


# --- sem_chave: a loud refusal that costs one sentence ----------------------------------------


def test_sem_chave_exige_motivo():
    p = _problemas(_contrato({"estado": "sem_chave", "chaves": []}))
    assert any("grao.motivo" in m for m in p)


def test_sem_chave_com_motivo_passa():
    c = _contrato(
        {
            "estado": "sem_chave",
            "chaves": [],
            "motivo": "Aggregate without identity: one row per "
            "request, the origin exposes no identifier.",
        }
    )
    assert _problemas(c) == []
    g = C.grao(c)
    assert g.estado == "sem_chave"
    assert g.chaves == ()
    assert g.motivo


def test_sem_chave_com_chaves_e_contradicao():
    c = _contrato({"estado": "sem_chave", "chaves": ["codigo"], "motivo": "none"})
    assert any("empty when estado='sem_chave'" in m for m in _problemas(c))


def test_motivo_em_branco_nao_conta():
    """Whitespace is the cheap way around the mandatory sentence."""
    c = _contrato({"estado": "sem_chave", "chaves": [], "motivo": "   "})
    assert any("grao.motivo" in m for m in _problemas(c))


def test_desconhecido_com_chaves_e_erro():
    c = _contrato({"estado": "desconhecido", "chaves": ["codigo"]})
    assert any("only applies with estado='declarado'" in m for m in _problemas(c))


def test_chaves_que_nao_e_lista_e_erro():
    assert any("list" in m for m in _problemas(_contrato({"estado": "declarado", "chaves": "a"})))


# --- which sources declare: a MEASURED decision, not scattered at will ------------------------


def test_so_declara_grao_quem_teve_a_chave_MEDIDA():
    """Written by hand on purpose, in the same mould as `test_so_declara_quem_nao_tem_data_na_origem`
    (`test_ingestao_competencia.py`): promoting a dataset forces an edit of this list, and a new
    source without a grain breaks the test instead of being born `desconhecido` silently.

    Guessing a key is worse than not declaring one: a wrong key yields a plausible but false
    duplicate count, and someone would decide something based on it.
    """
    from cflow_core.ingestao.catalogo import CATALOGO

    declaram = {ds for ds, c in CATALOGO.items() if C.grao(c).declarado}
    assert declaram == {"rates_daily", "registry_daily", "registry_monthly"}

    desconhecidos = {ds for ds, c in CATALOGO.items() if C.grao(c).desconhecido}
    assert desconhecidos == {"fx_quotes", "reports_portal"}
    assert declaram | desconhecidos == set(CATALOGO)


def test_todo_contrato_do_catalogo_continua_valido():
    """The grain declaration goes through the same validation as any contract, including the
    check against `schema_esperado`, which is exactly where a typo would get in."""
    from cflow_core.ingestao.catalogo import CATALOGO

    for ds, c in CATALOGO.items():
        try:
            C.validar(c)
        except ErroAplicacao as e:
            pytest.fail(f"{ds}: {[d.mensagem for d in (e.detalhes or [])]}")


def test_quem_declara_competencia_no_catalogo_projeta_a_fatia():
    """Coherence guard between the two fields: the key only exists because the slice becomes a column."""
    from cflow_core.ingestao.catalogo import CATALOGO

    for ds, c in CATALOGO.items():
        if "competencia" in C.grao(c).chaves:
            assert C.fatia_e_competencia(c), ds
