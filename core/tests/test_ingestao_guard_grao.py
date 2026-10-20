"""Grain uniqueness guard, on the publishing path.

The lesson behind these tests: a guard function can exist, have green tests and **never be
called**, leaving every pipeline to run without a guard while the docstring claims the opposite.
So besides testing the guard, these tests check that it **runs on the publishing path** and that
it runs **before** the data reaches the bucket.

`iceberg.publicar` is BLOCKING: it only acts on tables that get republished.
"""

from __future__ import annotations

import duckdb
import pyarrow as pa
import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.ingestao import gerador_silver as G
from cflow_core.ingestao import iceberg as ICE
from cflow_core.pipelines.definicao import validar

# --- Harness -------------------------------------------------------------------------------

DECLARADO = {"estado": "declarado", "chaves": ["codigo", "_competencia"], "motivo": None}


def _tabela(linhas: list[tuple]) -> pa.Table:
    return pa.table(
        {
            "codigo": [ln[0] for ln in linhas],
            "_competencia": [ln[1] for ln in linhas],
            "pu": [ln[2] for ln in linhas],
        }
    )


def _colunas(*nomes: str) -> list[G.ColunaSilver]:
    return [
        G.ColunaSilver(origem=n.lstrip("_"), destino=n, tipo="texto", expressao="x") for n in nomes
    ]


# --- Blocking: iceberg.publicar ------------------------------------------------------------


def test_grao_declarado_e_respeitado_nao_bloqueia():
    g, nulas = ICE.conferir_grao(
        dataset="registry_daily",
        tabela_arrow=_tabela([("A", "2026-01", 1.0), ("B", "2026-01", 2.0)]),
        grao=DECLARADO,
    )
    assert g.declarado and g.chaves == ("codigo", "_competencia")
    assert nulas == {}


def test_grao_violado_recusa_nomeando_dataset_chave_e_quantas_linhas():
    """The message must say HOW MANY rows collide: that is the number that decides the next step
    (one duplicate is the origin resubmitting the slice; half the table is the pipeline reading
    twice)."""
    duplicada = _tabela(
        [("A", "2026-01", 1.0), ("A", "2026-01", 9.0), ("B", "2026-01", 2.0), ("B", "2026-01", 3.0)]
    )
    with pytest.raises(ErroAplicacao) as e:
        ICE.conferir_grao(dataset="registry_daily", tabela_arrow=duplicada, grao=DECLARADO)
    assert e.value.codigo == "silver_grao_violado"
    assert e.value.status == 409
    m = e.value.mensagem
    assert "registry_daily" in m
    assert (
        "codigo, _competencia" in m
    )  # the column list must NOT get mangled by the thousands separator
    assert "4 of 4 row(s)" in m
    assert "2 group(s)" in m
    assert "2 excess row(s)" in m


def test_chave_ausente_no_resultado_e_erro_nomeado_nao_keyerror():
    """A declared key that does not exist in the published table is a registration typo. Letting
    Arrow's `KeyError` blow up would hand the operator a message with no dataset and no code."""
    sem_competencia = pa.table({"codigo": ["A"], "pu": [1.0]})
    with pytest.raises(ErroAplicacao) as e:
        ICE.conferir_grao(dataset="d", tabela_arrow=sem_competencia, grao=DECLARADO)
    assert e.value.codigo == "silver_grao_sem_coluna"
    assert "_competencia" in e.value.mensagem


@pytest.mark.parametrize(
    "grao",
    [
        None,
        {"estado": "desconhecido"},
        {"estado": "sem_chave", "motivo": "repeatable event without identity"},
    ],
)
def test_sem_grao_declarado_nao_bloqueia(grao):
    """Blocking on `desconhecido` would stop publishing most datasets over a declaration nobody
    measured. What the state does is get recorded and become a finding in the checker."""
    duplicada = _tabela([("A", "2026-01", 1.0), ("A", "2026-01", 1.0)])
    g, nulas = ICE.conferir_grao(dataset="d", tabela_arrow=duplicada, grao=grao)
    assert not g.declarado
    assert nulas == {}


def test_uma_linha_de_chave_nula_nao_bloqueia_e_e_medida():
    """A single null-key row goes unnoticed (group of size 1): it must not block publishing over a
    record no current consumer uses, and it has to be counted (`identifier-field-ids` requires
    `required=True`)."""
    com_nula = _tabela([("A", "2026-01", 1.0), ("B", "2026-01", 2.0), (None, "2026-01", 3.0)])
    g, nulas = ICE.conferir_grao(dataset="d", tabela_arrow=com_nula, grao=DECLARADO)
    assert g.declarado
    assert nulas == {"codigo": 1}


def test_duas_linhas_de_chave_nula_sao_medidas_nao_confundidas_com_duplicata():
    """`group_by` puts the two null rows in one group, the same blindness an ad-hoc
    `count(DISTINCT)` has, only inside the guard itself."""
    duas_nulas = _tabela([(None, "2026-01", 1.0), (None, "2026-01", 2.0), ("B", "2026-01", 3.0)])
    with pytest.raises(ErroAplicacao) as e:
        ICE.conferir_grao(dataset="d", tabela_arrow=duas_nulas, grao=DECLARADO)
    # It still blocks (TWO null-key rows really collide), but the message must say that nulls are
    # involved, so the reader does not mistake it for a duplicate of real data.
    assert "null-key" in e.value.mensagem


def test_desempate_declarado_resolve_a_reapresentacao_e_o_guard_para_de_bloquear():
    """End to end with a real DuckDB: the SQL `montar_sql` generates with `desempate` runs and the
    result passes the guard without collision: the origin resubmitted the record and the most
    recent version (`data_entrega DESC`) won."""
    colunas = [
        G.ColunaSilver("Codigo", "codigo", "texto", '"Codigo"'),
        G.ColunaSilver("Competencia", "_competencia", "texto", '"Competencia"'),
        G.ColunaSilver("Data_Entrega", "data_entrega", "texto", '"Data_Entrega"'),
    ]
    grao_contrato = {
        "estado": "declarado",
        "chaves": ["codigo", "competencia"],
        "desempate": {
            "colunas": [{"coluna": "data_entrega", "ordem": "desc"}],
            "motivo": "the origin resubmits the same record in a later delivery",
        },
    }
    g_saida = G.grao_para_saida(grao_contrato, colunas)
    sql = G.montar_sql(colunas, '"bronze"', grao_saida=g_saida)

    con = duckdb.connect()
    con.execute(
        "CREATE TABLE bronze AS SELECT * FROM (VALUES "
        "('A', '2019-09', '2019-10-31'), ('A', '2019-09', '2019-11-29')"
        ") AS t(Codigo, Competencia, Data_Entrega)"
    )
    resultado = con.execute(sql).to_arrow_table()
    assert resultado.num_rows == 1
    assert resultado.column("data_entrega").to_pylist() == ["2019-11-29"]

    g, nulas = ICE.conferir_grao(dataset="d", tabela_arrow=resultado, grao=g_saida)
    assert g.declarado
    assert nulas == {}


def test_empate_residual_sobrevive_ao_qualify_e_o_guard_recusa():
    """`row_number()` would hide this: it would pick an arbitrary row and the duplicate would
    vanish silently. With `rank()`, two deliveries on the SAME day tie, both survive the SQL and
    the guard refuses, handing back to the contract the case the declared tie-break does not
    solve instead of pretending it did."""
    colunas = [
        G.ColunaSilver("Codigo", "codigo", "texto", '"Codigo"'),
        G.ColunaSilver("Competencia", "_competencia", "texto", '"Competencia"'),
        G.ColunaSilver("Data_Entrega", "data_entrega", "texto", '"Data_Entrega"'),
    ]
    grao_contrato = {
        "estado": "declarado",
        "chaves": ["codigo", "competencia"],
        "desempate": {
            "colunas": [{"coluna": "data_entrega", "ordem": "desc"}],
            "motivo": "the origin resubmits the same record in a later delivery",
        },
    }
    g_saida = G.grao_para_saida(grao_contrato, colunas)
    sql = G.montar_sql(colunas, '"bronze"', grao_saida=g_saida)

    con = duckdb.connect()
    con.execute(
        "CREATE TABLE bronze AS SELECT * FROM (VALUES "
        "('A', '2019-09', '2019-11-29'), ('A', '2019-09', '2019-11-29')"
        ") AS t(Codigo, Competencia, Data_Entrega)"
    )
    resultado = con.execute(sql).to_arrow_table()
    assert resultado.num_rows == 2  # the tie SURVIVES: this is not a silent dedup

    with pytest.raises(ErroAplicacao) as e:
        ICE.conferir_grao(dataset="d", tabela_arrow=resultado, grao=g_saida)
    assert e.value.codigo == "silver_grao_violado"


async def test_publicar_recusa_ANTES_de_abrir_o_catalogo():
    """The guard must run before any access to the lake: a guard that runs after the data is in
    the bucket is not a guard, it is a report.

    `lake=None` blows up in `abrir_catalogo`; if the error that comes out is the grain one, the
    publication stopped before getting there."""
    duplicada = _tabela([("A", "2026-01", 1.0), ("A", "2026-01", 2.0)])
    with pytest.raises(ErroAplicacao) as e:
        await ICE.publicar(
            lake=None, dataset="registry_daily", tabela_arrow=duplicada, grao=DECLARADO
        )
    assert e.value.codigo == "silver_grao_violado"


async def test_publicar_com_grao_respeitado_segue_para_o_catalogo():
    """The counter-test of the previous one: without a collision, publishing does NOT stop at the
    guard; it reaches the catalogue (and only breaks there, because the test lake is fake).
    Without this, a guard that refused everything would pass the test above."""
    limpa = _tabela([("A", "2026-01", 1.0), ("B", "2026-01", 2.0)])

    class LakeFalso:  # has the shape of an Armazenamento, but is neither backend
        def raiz_uri(self, camada: str) -> str:
            return f"file:///tmp/{camada}"

    with pytest.raises(ErroAplicacao) as e:
        await ICE.publicar(lake=LakeFalso(), dataset="d", tabela_arrow=limpa, grao=DECLARADO)
    assert e.value.codigo == "iceberg_backend_nao_suportado"


# --- The grain travels in the pipeline definition ------------------------------------------


def test_grao_do_contrato_e_traduzido_para_o_nome_da_coluna_na_silver():
    """The contract says `competencia` (name in the bronze); the silver publishes `_competencia`
    (provenance prefix). Without the translation, the guard would look for a column that does not
    exist."""
    saida = G.grao_para_saida(
        {"estado": "declarado", "chaves": ["codigo", "competencia"]},
        _colunas("codigo", "_competencia", "pu"),
    )
    assert saida == {"estado": "declarado", "chaves": ["codigo", "_competencia"], "motivo": None}


def test_grao_ausente_vira_desconhecido_explicito_nunca_todas_as_colunas():
    assert G.grao_para_saida(None, _colunas("codigo")) == {
        "estado": "desconhecido",
        "chaves": [],
        "motivo": None,
    }


def test_chave_que_nao_existe_na_silver_quebra_a_geracao():
    """Fail loudly at generation: a key matching no column would produce a guard that never flags
    anything and looks like coverage."""
    with pytest.raises(ErroAplicacao) as e:
        G.grao_para_saida({"estado": "declarado", "chaves": ["nao_existe"]}, _colunas("codigo"))
    assert e.value.codigo == "silver_grao_sem_coluna"


def test_definicao_do_pipeline_carrega_o_grao_no_no_de_saida_e_continua_valida():
    d = G.definicao_pipeline(
        fonte_id="11111111-1111-1111-1111-111111111111",
        dataset_bronze="registry_daily",
        dataset_silver="registry_daily",
        colunas=_colunas("codigo", "_competencia"),
        grao={"estado": "declarado", "chaves": ["codigo", "competencia"]},
    )
    saida = next(n for n in d["nos"] if n["tipo"] == "saida")
    assert saida["config"]["grao"]["chaves"] == ["codigo", "_competencia"]
    validar(d)  # the output node with `grao` still passes structural validation


def test_definicao_do_pipeline_com_desempate_gera_qualify_e_continua_valida():
    """`definicao_pipeline` is the real junction point: contract -> output node -> `sql` node SQL.
    Without this test, the `QUALIFY` generated by `montar_sql` might never reach the real pipeline
    (the same lesson as a guard that exists, has green tests and is never called)."""
    d = G.definicao_pipeline(
        fonte_id="11111111-1111-1111-1111-111111111111",
        dataset_bronze="registry_monthly",
        dataset_silver="registry_current",
        colunas=_colunas("entity_id", "reference_code", "data_referencia", "data_entrega"),
        grao={
            "estado": "declarado",
            "chaves": [
                "entity_id",
                "reference_code",
                "data_referencia",
            ],
            "desempate": {
                "colunas": [{"coluna": "data_entrega", "ordem": "desc"}],
                "motivo": "the origin resubmits the same record in a later delivery",
            },
        },
    )
    validar(d)
    saida = next(n for n in d["nos"] if n["tipo"] == "saida")
    assert saida["config"]["grao"]["desempate"]["colunas"] == [
        {"coluna": "data_entrega", "ordem": "desc"}
    ]
    sql_no = next(n for n in d["nos"] if n["id"] == "tipado")
    assert "QUALIFY rank() OVER" in sql_no["sql"]
    assert '"data_entrega" DESC NULLS LAST' in sql_no["sql"]


def test_pipeline_sem_grao_declarado_nao_inventa_bloco():
    """Absence and `desconhecido` are the same thing for the reader (`ler_grao` normalises one into
    the other), so the empty block does not enter the versioned definition. What matters is that
    the result is the SAME on both paths."""
    d = G.definicao_pipeline(
        fonte_id="11111111-1111-1111-1111-111111111111",
        dataset_bronze="fx_quotes",
        dataset_silver="fx_quotes",
        colunas=_colunas("codigo"),
    )
    saida = next(n for n in d["nos"] if n["tipo"] == "saida")
    assert "grao" not in saida["config"]
    assert ICE.ler_grao({"grao": saida["config"].get("grao")}).estado == "desconhecido"
