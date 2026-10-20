"""Bronze->silver pipeline generator.

The generator is conservative on purpose: it produces an honest DRAFT, not a finished dataset.
Typing too much is worse than typing too little: a failed `TRY_CAST` becomes NULL silently, and
whatever we do not know how to convert must show up marked for a domain expert to decide.
"""

from __future__ import annotations

import duckdb
import pytest

from cflow_core.ingestao import gerador_silver as G
from cflow_core.pipelines.definicao import validar


@pytest.fixture
def con():
    c = duckdb.connect()
    yield c
    c.close()


def _tabela(con, linhas: list[tuple], colunas: list[str]) -> list[str]:
    """Sample table with ALL columns `VARCHAR`, like the real bronze.

    The explicit cast is not decoration: without it DuckDB types an all-NULL column as `INTEGER`,
    and the generator, which assumes the "bronze is faithful to the source and all text" contract,
    would fail in a scenario that does not exist in production."""
    valores = ", ".join(
        "(" + ", ".join("NULL" if v is None else f"'{v}'" for v in ln) + ")" for ln in linhas
    )
    cols = ", ".join(f'"{c}"' for c in colunas)
    projecao = ", ".join(f'CAST("{c}" AS VARCHAR) AS "{c}"' for c in colunas)
    con.execute(
        f"CREATE OR REPLACE TABLE t AS SELECT {projecao} FROM (VALUES {valores}) AS x({cols})"
    )
    return colunas


@pytest.mark.parametrize(
    ("bruto", "esperado"),
    [
        ("Tax_Id_Issuer", "tax_id_issuer"),
        ("Due Dt./  Amt.", "due_dt_amt"),
        ("Indicative Rate", "indicative_rate"),
        ("Certificate_Identification_Code", "certificate_identification_code"),
        ("Variação Diária(%)", "variacao_diaria"),
    ],
)
def test_nome_de_coluna_vira_snake_case_sem_acento(bruto: str, esperado: str) -> None:
    """Silver is consumed from outside: a name with spaces, dots or accents forces every consumer
    to quote it, and disappears in tools that do not support it."""
    assert G.normalizar_nome(bruto) == esperado


def test_fixed_preco_escala_e_codigo_fica_texto(con) -> None:
    """The case that motivated the declared profile: right type, value wrong by 100x."""
    cols = _tabela(
        con,
        [("20260814", "0000000003227", "00350", "ABCD4")],
        ["TRADE_DATE", "LASTPRICE", "TOTTRADES", "TICKER"],
    )
    inf = {c.origem: c for c in G.inferir_colunas(con, "t", cols, "fixed_quotes_daily")}
    assert inf["TRADE_DATE"].tipo == "data"
    assert inf["LASTPRICE"].tipo == "numero" and "/ 100" in inf["LASTPRICE"].expressao
    assert inf["TOTTRADES"].tipo == "numero" and "/ 100" not in inf["TOTTRADES"].expressao
    assert inf["TICKER"].tipo == "texto"
    # And the value actually comes out right, not just the expression looking right.
    sql = G.montar_sql(list(inf.values()), "t")
    assert float(con.execute(sql).fetchone()[1]) == pytest.approx(32.27)


def test_coluna_que_nao_converte_100_por_cento_fica_texto(con) -> None:
    """One bad row in a thousand is enough NOT to type: typing anyway would lose that row silently
    (it becomes NULL), which is the most expensive failure mode."""
    cols = _tabela(con, [("10.5",), ("20.1",), ("unavailable",)], ["TOTAL_AMOUNT"])
    inf = G.inferir_colunas(con, "t", cols, "monthly_registry")
    assert inf[0].tipo == "texto"
    assert "does not convert" in inf[0].motivo


def test_coluna_toda_nula_nao_e_tipada(con) -> None:
    """With not a single value, asserting a type is a guess."""
    cols = _tabela(con, [(None,), (None,)], ["SOME_AMOUNT"])
    inf = G.inferir_colunas(con, "t", cols, "monthly_registry")
    assert inf[0].tipo == "texto"


def test_identificador_convertivel_ainda_assim_fica_texto(con) -> None:
    """`00123` converts 100 % to a number, and would become 123, no longer the same identifier."""
    cols = _tabela(con, [("00123",), ("00456",)], ["CD_ENTITY"])
    inf = G.inferir_colunas(con, "t", cols, "monthly_registry")
    assert inf[0].tipo == "texto"
    assert "identifier" in inf[0].motivo


def test_proveniencia_entra_com_prefixo(con) -> None:
    """`ingestion_date` comes from hive partitioning: it is provenance, and the `_` prefix avoids
    colliding with a business column of the same name."""
    cols = _tabela(con, [("x", "2026-08-15")], ["tax_id", "ingestion_date"])
    inf = {c.origem: c for c in G.inferir_colunas(con, "t", cols, "monthly_registry")}
    assert inf["ingestion_date"].destino == "_ingestion_date"
    assert "provenance" in inf["ingestion_date"].motivo


def test_sql_gerado_roda_e_a_ultima_coluna_nao_tem_virgula(con) -> None:
    cols = _tabela(con, [("2026-01-01", "10.5", "ABC")], ["DT_REF", "UNIT_PRICE", "ENTITY_NAME"])
    inf = G.inferir_colunas(con, "t", cols, "daily_prices")
    sql = G.montar_sql(inf, "t")
    linhas = con.execute(sql).fetchall()
    assert len(linhas) == 1
    assert [d[0] for d in con.execute(sql).description] == [
        "dt_ref",
        "unit_price",
        "entity_name",
    ]


def test_sentinela_de_ausencia_nao_impede_a_tipagem(con) -> None:
    """Some sources write `--` for a missing rate and `N/D` for an unavailable price. Without
    handling that, a few rows would leave the whole column in VARCHAR, and the external consumer
    would receive the rate as text because of the days when the source said "none"."""
    cols = _tabela(con, [("0,693",), ("--",), ("1,205",)], ["Indicative Rate"])
    inf = G.inferir_colunas(con, "t", cols, "comma_bond_quotes")
    assert inf[0].tipo == "numero"
    valores = [r[0] for r in con.execute(G.montar_sql(inf, "t")).fetchall()]
    assert valores[1] is None  # the sentinel becomes NULL, which is what it means
    assert float(valores[0]) == pytest.approx(0.693)


def test_id_sem_pontuacao_nao_vira_numero_mesmo_sem_pista_no_nome(con) -> None:
    """`Risk_Holder` holds an unpunctuated tax ID (`00000000000000`): it converts 100 %, the name
    does not give it away, and typing erases the leading zero, producing a different ID. The data
    is what gives it away."""
    cols = _tabela(con, [("00000000000000",), ("29494037000103",)], ["Risk_Holder"])
    inf = G.inferir_colunas(con, "t", cols, "monthly_registry")
    assert inf[0].tipo == "texto"
    assert "identifier" in inf[0].motivo


def test_inteiro_longo_sem_zero_a_esquerda_tambem_e_identificador(con) -> None:
    """12+ digits is the same ruler the front-end grid uses to avoid thousands separators."""
    cols = _tabela(con, [("29494037000103",), ("43822395000110",)], ["Risk_Holder"])
    assert G.inferir_colunas(con, "t", cols, "monthly_registry")[0].tipo == "texto"


def test_fixed_com_padding_de_zero_continua_sendo_numero(con) -> None:
    """The format veto must NOT apply in the fixed-width layout: `0000000003227` is 32.27 and
    `000000030219856600` is a volume; the leading zero there is padding, not an identifier."""
    cols = _tabela(con, [("0000000003227", "000000030219856600")], ["LASTPRICE", "TOTVOL"])
    inf = {c.origem: c for c in G.inferir_colunas(con, "t", cols, "fixed_quotes_daily")}
    assert inf["LASTPRICE"].tipo == "numero" and inf["TOTVOL"].tipo == "numero"


def test_coluna_que_ja_chega_tipada_da_origem_passa_intacta(con) -> None:
    """Bronze is *almost* always text: a JSON API's `quotedAt` arrives as `TIMESTAMP` and Parquet
    preserves it. Applying `trim()` to it does not even compile, and recasting through text would
    be a gratuitous risk."""
    con.execute(
        "CREATE OR REPLACE TABLE t AS SELECT "
        "TIMESTAMP '2026-08-14 13:00:00' AS \"quotedAt\", "
        '5.0717::DOUBLE AS "sellRate", \'x\' AS "bulletinType"'
    )
    inf = {
        c.origem: c
        for c in G.inferir_colunas(
            con, "t", ["quotedAt", "sellRate", "bulletinType"], "api_fx_rates"
        )
    }
    assert inf["quotedAt"].tipo == "data"
    assert inf["sellRate"].tipo == "numero"
    assert inf["bulletinType"].tipo == "texto"
    assert inf["quotedAt"].expressao == '"quotedAt"'  # identity, no cast


def test_definicao_gerada_passa_na_validacao_do_grafo(con) -> None:
    """The artifact is a REAL pipeline: it must pass the same validation as any graph."""
    cols = _tabela(con, [("2026-01-01", "10.5")], ["Reference_Date", "TOTAL_AMOUNT"])
    inf = G.inferir_colunas(con, "t", cols, "monthly_registry")
    definicao = G.definicao_pipeline(
        fonte_id="11111111-1111-1111-1111-111111111111",
        dataset_bronze="monthly_registry",
        dataset_silver="registry",
        colunas=inf,
    )
    validar(definicao)  # does not raise
    assert definicao["nos"][0]["config"]["origem"] == "lake"
    # Iceberg is the default: consumers (query engines) read tables.
    assert definicao["nos"][2]["config"] == {"destino": "iceberg", "dataset": "registry"}


def test_saida_declara_a_conexao_do_lake(con) -> None:
    """Without `conexao_id`, the output falls back to the LOCAL backend (the air-gapped default):
    the node writes to the container disk, finishes without error and nothing shows up in the
    bucket. Silver must go to the same lake the bronze came from, and that is DECLARED in the
    versioned definition."""
    cols = _tabela(con, [("2026-01-01",)], ["Reference_Date"])
    inf = G.inferir_colunas(con, "t", cols, "monthly_registry")
    cx = "33333333-3333-3333-3333-333333333333"
    d = G.definicao_pipeline(
        fonte_id="11111111-1111-1111-1111-111111111111",
        dataset_bronze="monthly_registry",
        dataset_silver="registry",
        colunas=inf,
        conexao_id=cx,
    )
    validar(d)
    assert d["nos"][2]["config"]["conexao_id"] == cx


def test_corte_por_fatia_entra_na_definicao(con) -> None:
    """The knob that separates the annual file from the daily one, which overlap in the current
    year."""
    cols = _tabela(con, [("20260814",)], ["TRADE_DATE"])
    inf = G.inferir_colunas(con, "t", cols, "fixed_quotes_annual")
    d = G.definicao_pipeline(
        fonte_id="22222222-2222-2222-2222-222222222222",
        dataset_bronze="fixed_quotes_annual",
        dataset_silver="quotes",
        colunas=inf,
        chave_max="2025",
    )
    validar(d)
    assert d["nos"][0]["config"]["chave_max"] == "2025"


def test_gerador_ainda_sabe_emitir_o_destino_lake(con) -> None:
    """The `lake` destination was not removed: whoever wants Parquet with a pointer still can."""
    cols = _tabela(con, [("2026-01-01",)], ["Reference_Date"])
    inf = G.inferir_colunas(con, "t", cols, "monthly_registry")
    d = G.definicao_pipeline(
        fonte_id="11111111-1111-1111-1111-111111111111",
        dataset_bronze="monthly_registry",
        dataset_silver="registry",
        colunas=inf,
        destino="lake",
    )
    validar(d)
    assert d["nos"][2]["config"]["destino"] == "lake"
