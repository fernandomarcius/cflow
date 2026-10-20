"""Reading a published dataset by run - the PURE part: parsing of the route parameters, DuckDB
query over the exact list of parquets and selection of index partitions.

No database and no lake: the parquets are written to `tmp_path` and the query runs on them. The
resolution (run -> version -> `saida` node -> immutable index) is covered by the route integration
tests.
"""

from __future__ import annotations

import json
import math
from datetime import date

import duckdb
import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.ingestao import publicados


def _parquet(tmp_path, nome: str, sql: str) -> str:
    destino = tmp_path / f"{nome}.parquet"
    duckdb.connect().execute(f"COPY ({sql}) TO '{destino}' (FORMAT parquet)")
    return str(destino)


def _consultar(caminhos, **kw):
    base = {"filtros": [], "ordem": [], "agregados": [], "skip": 0, "take": 100}
    return publicados.consultar(caminhos, **{**base, **kw})


async def test_abrir_publicacao_dev_recusa_manifesto_apontando_prd(monkeypatch):
    execucao_id = "11111111-1111-4111-8111-111111111111"

    class Lake:
        async def ler(self, camada, caminho):
            assert camada == "gold"
            assert caminho == f"dev/ref/01/summary/execucao_id={execucao_id}/_manifesto.json"
            return json.dumps(
                {
                    "dataset": "ref/01/summary",
                    "camada": "gold",
                    "execucao_id": execucao_id,
                    "parametros": {"ambiente": "dev"},
                    "particao": {},
                    "caminho": (f"prd/ref/01/summary/execucao_id={execucao_id}/dados.parquet"),
                }
            ).encode()

    async def abrir(_conexao_id):
        return Lake()

    monkeypatch.setattr(publicados.armazenamento, "abrir", abrir)
    with pytest.raises(ErroAplicacao) as erro:
        await publicados.abrir_publicacao(
            {"id": execucao_id, "parametros": {"ambiente": "dev"}},
            {"conexao_id": "lake"},
            "gold",
            "ref/01/summary",
        )
    assert erro.value.codigo == "lake_ponteiro_invalido"


async def test_abrir_publicacao_recusa_indice_vazio_de_outra_execucao(monkeypatch):
    execucao_id = "11111111-1111-4111-8111-111111111111"

    class Lake:
        async def ler(self, _camada, _caminho):
            return json.dumps(
                {
                    "dataset": "ref/01/summary",
                    "camada": "gold",
                    "execucao_id": "22222222-2222-4222-8222-222222222222",
                    "parametros": {"ambiente": "dev"},
                    "chaves": ["operacao_id"],
                    "particoes": [],
                }
            ).encode()

    async def abrir(_conexao_id):
        return Lake()

    monkeypatch.setattr(publicados.armazenamento, "abrir", abrir)
    with pytest.raises(ErroAplicacao) as erro:
        await publicados.abrir_publicacao(
            {
                "id": execucao_id,
                "parametros": {"ambiente": "dev", "operacao_id": "2"},
                "tipos_parametro": {"operacao_id": "texto"},
            },
            {
                "conexao_id": "lake",
                "particao": [{"origem": "parametro", "nome": "operacao_id"}],
            },
            "gold",
            "ref/01/summary",
        )
    assert erro.value.codigo == "lake_ponteiro_invalido"


# --- parsing of the route parameters --------------------------------------------------------------


def test_filtro_igualdade_e_contem():
    assert publicados.interpretar_filtros(["project=ACME", "nome~silva"]) == [
        ("project", "=", "ACME"),
        ("nome", "~", "silva"),
    ]


def test_filtro_separa_no_primeiro_operador_e_preserva_o_resto_do_valor():
    """The VALUE may contain `=` and `~` (free text); the column may not. Splitting at the last
    operator would send part of the value into the column name."""
    assert publicados.interpretar_filtros(["obs=a=b~c"]) == [("obs", "=", "a=b~c")]
    assert publicados.interpretar_filtros(["obs~x=y"]) == [("obs", "~", "x=y")]


@pytest.mark.parametrize("bruto", ["nooperator", "=valor", "~valor", ""])
def test_filtro_malformado_e_400(bruto):
    with pytest.raises(ErroAplicacao) as e:
        publicados.interpretar_filtros([bruto])
    assert e.value.status == 400
    assert e.value.codigo == "filtro_invalido"


def test_ordenar_asc_e_desc():
    assert publicados.interpretar_ordenar(["project", "-valor"]) == [
        ("project", False),
        ("valor", True),
    ]


@pytest.mark.parametrize("bruto", ["", "-"])
def test_ordenar_vazio_e_400(bruto):
    with pytest.raises(ErroAplicacao) as e:
        publicados.interpretar_ordenar([bruto])
    assert e.value.codigo == "ordenacao_invalida"


def test_agregar_sum_varias_colunas():
    assert publicados.interpretar_agregar(["sum:Valor,Juros"]) == [
        ("sum", "Valor"),
        ("sum", "Juros"),
    ]


def test_agregar_count_distinct_repetivel_e_combinavel_com_sum():
    """A distinct-order count over the slice: a thin client counts nothing, so the function lives
    here."""
    assert publicados.interpretar_agregar(
        ["sum:Valor,Juros", "count_distinct:Order_Number", "count_distinct:ProjectId"]
    ) == [
        ("sum", "Valor"),
        ("sum", "Juros"),
        ("count_distinct", "Order_Number"),
        ("count_distinct", "ProjectId"),
    ]


@pytest.mark.parametrize(
    "bruto",
    ["avg:Valor", "Valor", "sum:", "sum:,", "count:Valor", "distinct:Valor", "count_distinct:"],
)
def test_agregar_fora_do_vocabulario_e_400(bruto):
    """CLOSED vocabulary: `avg`/`max`/`count` silently accepted and without effect would be worse
    than refused - only `sum` and `count_distinct` have a declared consumer."""
    with pytest.raises(ErroAplicacao) as e:
        publicados.interpretar_agregar([bruto])
    assert e.value.codigo == "agregacao_invalida"


@pytest.mark.parametrize(("skip", "take"), [(-1, 10), (0, 0), (0, 1001)])
def test_paginacao_fora_dos_limites_e_400(skip, take):
    with pytest.raises(ErroAplicacao) as e:
        publicados.validar_paginacao(skip, take)
    assert e.value.status == 400
    assert e.value.codigo == "paginacao_invalida"


def test_paginacao_nos_limites_passa():
    publicados.validar_paginacao(0, 1)
    publicados.validar_paginacao(10, 1000)


# --- values that come out in JSON ------------------------------------------------------------------


def test_valores_json_decimal_data_e_nan():
    from decimal import Decimal

    assert publicados.valor_json(Decimal("12.50")) == "12.50"
    assert publicados.valor_json(Decimal("0E-10")) == "0.0000000000"  # never scientific notation
    assert publicados.valor_json(date(2026, 8, 1)) == "2026-08-01"
    assert publicados.valor_json(math.nan) is None
    assert publicados.valor_json(math.inf) is None
    assert publicados.valor_json(1.5) == 1.5
    assert publicados.valor_json([Decimal("1.0"), None]) == ["1.0", None]


# --- query over the exact list of parquets --------------------------------------------------


def test_paginacao_e_total_do_conjunto(tmp_path):
    arq = _parquet(tmp_path, "p", "SELECT CAST(range AS INTEGER) AS n FROM range(25)")
    r = _consultar([arq], ordem=[("n", False)], skip=10, take=5)
    assert r["total"] == 25
    assert [linha["n"] for linha in r["linhas"]] == [10, 11, 12, 13, 14]
    assert r["colunas"] == [{"nome": "n", "tipo": "INTEGER"}]


def test_varios_arquivos_sao_um_conjunto_so(tmp_path):
    a = _parquet(tmp_path, "a", "SELECT CAST(range AS INTEGER) AS n FROM range(3)")
    b = _parquet(tmp_path, "b", "SELECT CAST(range + 3 AS INTEGER) AS n FROM range(4)")
    r = _consultar([a, b], ordem=[("n", True)], take=2)
    assert r["total"] == 7
    assert [linha["n"] for linha in r["linhas"]] == [6, 5]


def test_caminho_hive_nao_vira_coluna(tmp_path):
    """The published parquet lives at `.../project_id=1201/execucao_id=<uuid>/dados.parquet`, and
    DuckDB AUTO-DETECTS hive partitioning from the path: without turning it off, `project_id` and
    `execucao_id` appeared as columns the file does not have - the response changed the schema of
    the published data."""
    pasta = tmp_path / "ds" / "operacao_id=61" / "project_id=1201" / "execucao_id=abc"
    pasta.mkdir(parents=True)
    arq = str(pasta / "dados.parquet")
    duckdb.connect().execute(f"COPY (SELECT 1201 AS PROJECTID, 10 AS valor) TO '{arq}'")
    r = _consultar([arq], filtros=[("PROJECTID", "=", "1201")])
    assert [c["nome"] for c in r["colunas"]] == ["PROJECTID", "valor"]
    assert r["linhas"] == [{"PROJECTID": 1201, "valor": 10}]
    vazio = _consultar([], caminho_esquema=arq)
    assert [c["nome"] for c in vazio["colunas"]] == ["PROJECTID", "valor"]


def test_filtro_hostil_vai_por_bind_e_nunca_entra_no_sql(tmp_path, monkeypatch):
    """The filter value is caller data: concatenated into the text it would become an injection. It is
    proven from BOTH sides - the result (0 rows, no error) and the text of every SQL executed."""
    arq = _parquet(tmp_path, "h", "SELECT * FROM (VALUES ('x'), ('y')) t(c)")
    hostil = "x' OR '1'='1"
    executados: list[str] = []
    original = publicados._abrir_duck

    class _Espiao:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *args, **kwargs):
            executados.append(sql)
            return self._con.execute(sql, *args, **kwargs)

        def close(self):
            self._con.close()

    monkeypatch.setattr(publicados, "_abrir_duck", lambda: _Espiao(original()))
    r = _consultar([arq], filtros=[("c", "=", hostil)])
    assert r["total"] == 0
    assert r["linhas"] == []
    assert executados, "the query must go through the connection opened by _abrir_duck"
    assert not any(hostil in sql or "OR '1'" in sql for sql in executados)


def test_filtro_igualdade_compara_como_texto(tmp_path):
    arq = _parquet(tmp_path, "i", "SELECT * FROM (VALUES (1201, 1), (1202, 2)) t(project, v)")
    r = _consultar([arq], filtros=[("project", "=", "1202")])
    assert r["total"] == 1
    assert r["linhas"][0]["v"] == 2


def test_igualdade_repetida_na_mesma_coluna_e_qualquer_um(tmp_path):
    """`project=A` + `project=C` is A OR C - by AND it would always be empty. A different column is
    still AND, and the aggregate (the footer sum) is over the same set of rows."""
    arq = _parquet(
        tmp_path,
        "q",
        "SELECT * FROM (VALUES ('A', 1), ('B', 2), ('C', 3), ('A', 4)) t(project, v)",
    )
    r = _consultar(
        [arq], filtros=[("project", "=", "A"), ("project", "=", "C")], agregados=[("sum", "v")]
    )
    assert r["total"] == 3
    assert sorted(linha["v"] for linha in r["linhas"]) == [1, 3, 4]
    assert r["agregados"] == {"sum": {"v": "8"}}

    misto = _consultar(
        [arq], filtros=[("project", "=", "A"), ("v", "=", "4"), ("PROJECT", "=", "B")]
    )
    assert [linha["v"] for linha in misto["linhas"]] == [
        4
    ]  # (A or B) AND v=4; the column case does not separate


def test_contem_repetido_continua_e(tmp_path):
    arq = _parquet(tmp_path, "e", "SELECT * FROM (VALUES ('ab'), ('a'), ('b')) t(s)")
    assert _consultar([arq], filtros=[("s", "~", "a"), ("s", "~", "b")])["total"] == 1


def test_igualdade_repetida_hostil_vai_por_bind(tmp_path):
    arq = _parquet(tmp_path, "hq", "SELECT * FROM (VALUES ('x'), ('y')) t(c)")
    r = _consultar([arq], filtros=[("c", "=", "x' OR '1'='1"), ("c", "=", "y")])
    assert [linha["c"] for linha in r["linhas"]] == ["y"]


def test_contem_trata_curinga_como_literal(tmp_path):
    """`~50%` looks for the text "50%", not "50" followed by anything: `%` and `_` in the value are
    escaped, otherwise the filter of whoever typed a percentage would bring rows that do not
    contain it."""
    arq = _parquet(
        tmp_path, "c", "SELECT * FROM (VALUES ('rate 50%'), ('rate 500'), ('a_b'), ('aXb')) t(s)"
    )
    assert _consultar([arq], filtros=[("s", "~", "50%")])["total"] == 1
    assert _consultar([arq], filtros=[("s", "~", "a_b")])["total"] == 1
    assert _consultar([arq], filtros=[("s", "~", "RATE")])["total"] == 2  # ILIKE


def test_coluna_inexistente_e_400_nomeando_a_coluna(tmp_path):
    arq = _parquet(tmp_path, "x", "SELECT 1 AS n")
    for kw in (
        {"filtros": [("missing_col", "=", "1")]},
        {"ordem": [("missing_col", False)]},
        {"agregados": [("sum", "missing_col")]},
    ):
        with pytest.raises(ErroAplicacao) as e:
            _consultar([arq], **kw)
        assert e.value.status == 400
        assert e.value.codigo == "coluna_inexistente"
        assert "missing_col" in e.value.mensagem


def test_coluna_casa_sem_caixa_quando_unica(tmp_path):
    arq = _parquet(tmp_path, "k", "SELECT 7 AS PROJECTID")
    assert _consultar([arq], filtros=[("projectid", "=", "7")])["total"] == 1


def test_soma_e_decimal_exata_sobre_o_conjunto_filtrado(tmp_path):
    """`sum(DOUBLE)` is neither deterministic nor exact (0.1 x 25 gives 2.5000000000000004); in
    DECIMAL it is. And the sum is over the whole FILTERED set, not the page - `take=1` does not
    change the aggregate."""
    arq = _parquet(
        tmp_path,
        "s",
        "SELECT CAST(range AS INTEGER) AS n, CAST(0.1 AS DOUBLE) AS v, "
        "CAST(1.25 AS DECIMAL(18,2)) AS d FROM range(25)",
    )
    r = _consultar([arq], agregados=[("sum", "v"), ("sum", "d"), ("sum", "n")], take=1)
    assert r["agregados"] == {"sum": {"v": "2.5000000000", "d": "31.25", "n": "300"}}
    filtrado = _consultar([arq], filtros=[("n", "=", "3")], agregados=[("sum", "d")], take=1)
    assert filtrado["agregados"] == {"sum": {"d": "1.25"}}


def test_agregados_por_funcao_sem_colisao_na_mesma_coluna(tmp_path):
    """Aggregates are grouped BY FUNCTION: the same column summed and counted does not overwrite
    itself (a flat `{column: value}` map would return only the last). `count_distinct` comes out as
    an integer in TEXT, like the sum, and NULL does not count - it is not an order."""
    arq = _parquet(
        tmp_path,
        "cd",
        "SELECT * FROM (VALUES ('V1', 10), ('V1', 20), ('V2', 30), (NULL, 40)) "
        "t(Order_Number, valor)",
    )
    r = _consultar(
        [arq],
        agregados=[
            ("sum", "valor"),
            ("count_distinct", "Order_Number"),
            ("count_distinct", "valor"),
        ],
        take=1,
    )
    assert r["agregados"] == {
        "sum": {"valor": "100"},
        "count_distinct": {"Order_Number": "2", "valor": "4"},
    }
    filtrado = _consultar(
        [arq],
        filtros=[("Order_Number", "=", "V1")],
        agregados=[("count_distinct", "Order_Number"), ("sum", "valor")],
    )
    assert filtrado["agregados"] == {
        "sum": {"valor": "30"},
        "count_distinct": {"Order_Number": "1"},
    }


def test_count_distinct_de_coluna_inexistente_e_400(tmp_path):
    arq = _parquet(tmp_path, "cdx", "SELECT 1 AS n")
    with pytest.raises(ErroAplicacao) as e:
        _consultar([arq], agregados=[("count_distinct", "Order_Number")])
    assert e.value.codigo == "coluna_inexistente"
    assert "Order_Number" in e.value.mensagem


def test_soma_de_texto_nao_numerico_e_400(tmp_path):
    arq = _parquet(tmp_path, "t", "SELECT 'abc' AS s")
    with pytest.raises(ErroAplicacao) as e:
        _consultar([arq], agregados=[("sum", "s")])
    assert e.value.codigo == "agregacao_invalida"
    assert "s" in e.value.mensagem


def test_soma_de_conjunto_vazio_e_nula(tmp_path):
    """The sum of nothing is NULL (there is no total to state); the count of nothing is 0 (it is a
    measurement: zero orders). It is the SQL semantics, and it is the same with or without a
    selected file."""
    arq = _parquet(tmp_path, "z", "SELECT CAST(1.5 AS DOUBLE) AS v")
    r = _consultar(
        [arq], filtros=[("v", "~", "nothing")], agregados=[("sum", "v"), ("count_distinct", "v")]
    )
    assert r["total"] == 0
    assert r["agregados"] == {"sum": {"v": None}, "count_distinct": {"v": "0"}}


def test_sem_arquivo_selecionado_usa_o_esquema_e_devolve_vazio(tmp_path):
    """A partition filter that matches nothing: the response still says WHICH columns the dataset
    has - otherwise "no rows" and "dataset without a schema" are indistinguishable."""
    arq = _parquet(tmp_path, "e", "SELECT 1 AS n")
    r = _consultar([], caminho_esquema=arq, agregados=[("sum", "n"), ("count_distinct", "n")])
    assert r["total"] == 0
    assert r["linhas"] == []
    assert r["colunas"] == [{"nome": "n", "tipo": "INTEGER"}]
    assert r["agregados"] == {"sum": {"n": None}, "count_distinct": {"n": "0"}}


# --- stable paging ------------------------------------------------------------------------

# 200 thousand rows in one file (or 4 x 50 thousand) with the sort key at only 3 values and a filter
# that leaves 5 thousand: this is the size at which DuckDB parallel Top-N already picks a DIFFERENT
# subset of the tied rows on each page (measured: 5 of 5 scans with repetition, before the
# tie-break). Below that, the read comes out stable by chance and the test does not discriminate.
_LINHAS_EMPATE = 200_000
_PASSO_FILTRO = 40


def _arquivos_empatados(tmp_path, n_arquivos: int) -> tuple[list[str], int]:
    por_arquivo = _LINHAS_EMPATE // n_arquivos
    caminhos = [
        _parquet(
            tmp_path,
            f"emp{i}",
            f"SELECT {i} * {por_arquivo} + r AS id, 'project_' || (r % 3) AS project, "
            f"(r % {_PASSO_FILTRO} = 0) AS sel FROM range({por_arquivo}) t(r)",
        )
        for i in range(n_arquivos)
    ]
    return caminhos, por_arquivo


def _varrer(caminhos, ordem, take=1000) -> tuple[list[int], int]:
    lidos: list[int] = []
    skip = 0
    while True:
        r = _consultar(caminhos, filtros=[("sel", "=", "true")], ordem=ordem, skip=skip, take=take)
        lidos.extend(linha["id"] for linha in r["linhas"])
        skip += take
        if skip >= r["total"]:
            return lidos, r["total"]


@pytest.mark.parametrize("n_arquivos", [1, 4])
def test_paginacao_ordenada_em_chave_empatada_nao_repete_nem_omite(tmp_path, n_arquivos):
    """`ordenar=project` (a screen column, almost never unique) with `LIMIT/OFFSET` in independent
    queries: without a tie-break, each page picked another subset of the tied rows - a row repeated
    on one page, a row missing on another, and `total` saying everything was there.
    The tie-break is (file, row in file), so the whole scan returns the EXACT set, without
    repeating, and in the SAME order every time."""
    caminhos, por_arquivo = _arquivos_empatados(tmp_path, n_arquivos)
    esperado = [
        i * por_arquivo + r
        for i in range(n_arquivos)
        for r in range(por_arquivo)
        if r % _PASSO_FILTRO == 0
    ]
    lidos, total = _varrer(caminhos, [("project", False)])
    assert total == len(esperado) == 5000
    assert len(lidos) == len(set(lidos)), "row repeated across pages"
    assert sorted(lidos) == sorted(esperado), "multiset of the scan != filtered set"
    # Within a tie, the order is that of the file (the names `emp0..emp3` sort like the list) and
    # that of the row in it - which, here, is the id order.
    assert lidos == sorted(esperado, key=lambda i: ((i % por_arquivo) % 3, i))
    assert _varrer(caminhos, [("project", False)])[0] == lidos, "the same query changed order"


def test_paginacao_decrescente_em_chave_empatada_tambem_desempata(tmp_path):
    caminhos, por_arquivo = _arquivos_empatados(tmp_path, 4)
    lidos, total = _varrer(caminhos, [("project", True)])
    assert len(lidos) == len(set(lidos)) == total
    # The tie-break is ASCENDING even with a descending key: it is not requested, it is the physical order.
    assert lidos == sorted(lidos, key=lambda i: (-((i % por_arquivo) % 3), i))


def test_sem_ordenar_a_ordem_e_arquivo_e_linha(tmp_path):
    """Without `ordenar`, stability is also EXPLICIT (file, row) and not a side effect of
    `preserve_insertion_order`, which is a connection setting and could change under the route."""
    caminhos, _ = _arquivos_empatados(tmp_path, 4)
    lidos, total = _varrer(list(reversed(caminhos)), [])
    assert lidos == sorted(lidos) and len(lidos) == total


def test_desempate_nao_vaza_colunas_na_resposta(tmp_path):
    arq = _parquet(tmp_path, "dv", "SELECT * FROM (VALUES (1, 'a'), (2, 'a')) t(n, project)")
    r = _consultar([arq], ordem=[("project", False)])
    assert [c["nome"] for c in r["colunas"]] == ["n", "project"]
    assert r["linhas"] == [{"n": 1, "project": "a"}, {"n": 2, "project": "a"}]


def test_coluna_com_nome_reservado_do_desempate_e_recusada_nomeando(tmp_path):
    """`file_row_number` is the name DuckDB gives to the row position, and it does not let it be
    renamed: a dataset with such a column would make the read blow up with a binder error. A named
    refusal, instead of a 500 or paging without a tie-break."""
    arq = _parquet(tmp_path, "rs", "SELECT 1 AS file_row_number, 2 AS x")
    with pytest.raises(ErroAplicacao) as e:
        _consultar([arq])
    assert e.value.codigo == "coluna_reservada"
    assert "file_row_number" in e.value.mensagem


# --- filter on a temporal column ----------------------------------------------------------------


def _temporal(tmp_path) -> str:
    return _parquet(
        tmp_path,
        "ts",
        "SELECT * FROM (VALUES "
        "(TIMESTAMP '2026-08-06 12:17:23', DATE '2026-08-06', 1), "
        "(TIMESTAMP '2026-08-06 12:17:23.5', DATE '2026-08-07', 2), "
        "(TIMESTAMP '2026-08-07 00:00:00', DATE '2026-08-08', 3)) t(dataRegistro, dia, n)",
    )


def test_filtro_igual_em_coluna_temporal_casa_o_valor_que_a_propria_resposta_devolve(tmp_path):
    """The row comes out with `isoformat()` (`2026-08-06T12:17:23`) and DuckDB CAST to text uses a
    space: comparing as text made the value copied from the response itself return `total: 0`,
    with no error. On a temporal column the comparison is in the column TYPE, and both forms
    match."""
    arq = _temporal(tmp_path)
    linhas = {linha["n"]: linha for linha in _consultar([arq])["linhas"]}
    assert linhas[1]["dataRegistro"] == "2026-08-06T12:17:23"
    for n, linha in linhas.items():
        for coluna in ("dataRegistro", "dia"):
            r = _consultar([arq], filtros=[(coluna, "=", linha[coluna])])
            assert [x["n"] for x in r["linhas"]] == [n], (coluna, linha[coluna])
    assert _consultar([arq], filtros=[("dataRegistro", "=", "2026-08-06 12:17:23")])["total"] == 1


@pytest.mark.parametrize("valor", ["yesterday", "", "2026-13-01"])
def test_filtro_igual_em_coluna_temporal_que_nao_converte_e_400_nomeando(tmp_path, valor):
    """A value that is not a date/instant is not "zero rows" - it is a wrong request, and zero rows
    would be the plausible and wrong result."""
    arq = _temporal(tmp_path)
    with pytest.raises(ErroAplicacao) as e:
        _consultar([arq], filtros=[("dataRegistro", "=", valor)])
    assert e.value.status == 400
    assert e.value.codigo == "filtro_invalido"
    assert "dataRegistro" in e.value.mensagem and repr(valor) in e.value.mensagem


def test_nan_e_data_saem_como_json(tmp_path):
    arq = _parquet(
        tmp_path,
        "j",
        "SELECT CAST('nan' AS DOUBLE) AS z, DATE '2026-08-01' AS dt, "
        "CAST(3.10 AS DECIMAL(18,2)) AS d",
    )
    linha = _consultar([arq])["linhas"][0]
    assert linha == {"z": None, "dt": "2026-08-01", "d": "3.10"}


# --- index partitions -----------------------------------------------------------------------


_PARTICOES = [
    {"particao": {"operacao_id": "61", "project_id": "1201"}, "caminho": "a", "linhas": 2},
    {"particao": {"operacao_id": "61", "project_id": "1202"}, "caminho": "b", "linhas": 1},
]


def _selecionar(filtros, **kw):
    base = {
        "base": {"operacao_id": "61"},
        "chaves_coluna": ["project_id"],
        "parametros": {
            "operacao_id": "61",
            "competencia_inicio": "2026-08-01",
            "competencia_fim": "2026-08-31",
        },
        "tipos_parametro": {"operacao_id": "texto", "competencia": "competencia"},
        "dataset": "ds",
        "execucao_id": "e1",
    }
    return publicados.selecionar_particoes(_PARTICOES, filtros, **{**base, **kw})


def test_sem_filtro_devolve_todas():
    assert _selecionar({}) == _PARTICOES


def test_filtro_de_coluna_seleciona_particoes():
    assert [p["caminho"] for p in _selecionar({"project_id": "1202"})] == ["b"]
    assert (
        _selecionar({"project_id": "9999"}) == []
    )  # the run did not publish that project: zero rows


def test_parametro_da_propria_execucao_casa_ou_e_404():
    """A PARAMETER partition is constant in the run: asking for another value is not "zero rows", it
    is asking the wrong run - and answering empty would be the plausible and wrong result."""
    assert _selecionar({"operacao_id": "61"}) == _PARTICOES
    with pytest.raises(ErroAplicacao) as e:
        _selecionar({"operacao_id": "62"})
    assert e.value.status == 404
    assert e.value.codigo == "publicado_ausente"
    assert "62" in e.value.mensagem and "61" in e.value.mensagem


def test_competencia_em_mes_casa_com_o_dia_1():
    """The `competencia` parameter arrives expanded in `competencia_inicio` (day 1); whoever calls
    thinks in months. `2026-08` and `2026-08-01` are the same request."""
    assert _selecionar({"competencia": "2026-08"}) == _PARTICOES
    assert _selecionar({"competencia": "2026-08-01"}) == _PARTICOES
    with pytest.raises(ErroAplicacao) as e:
        _selecionar({"competencia": "2026-07"})
    assert e.value.status == 404


def test_particao_de_parametro_competencia_casa_em_mes():
    parts = [{"particao": {"competencia": "2026-08-01"}, "caminho": "a", "linhas": 1}]
    sel = publicados.selecionar_particoes(
        parts,
        {"competencia": "2026-08"},
        base={"competencia": "2026-08-01"},
        chaves_coluna=[],
        parametros={},
        dataset="ds",
        execucao_id="e1",
    )
    assert sel == parts


def test_chave_que_nao_e_particao_nem_parametro_e_400():
    """A filter silently accepted and without effect would return the WHOLE run to whoever asked
    for one project."""
    with pytest.raises(ErroAplicacao) as e:
        _selecionar({"company": "7"})
    assert e.value.status == 400
    assert e.value.codigo == "filtro_particao_invalido"
    assert "company" in e.value.mensagem


@pytest.mark.parametrize("pedido", ["2026-08-01", "2026-08-15"])
def test_so_competencia_herda_o_inicio_da_janela(pedido):
    """`data` is neither a partition nor a parameter: the run declared `data_inicio`/`data_fim` of
    type `data`. Falling back to `<key>_inicio` without looking at the type accepted
    `data=2026-08-01` returning the WHOLE run, and with `2026-08-15` answered 404 claiming the run
    "published with data=2026-08-01" - a parameter that does not exist. The same rule as the engine
    (`_competencia_expandida`): only type `competencia` arrives expanded."""
    parts = [{"particao": {"operacao_id": "61"}, "caminho": "a", "linhas": 1}]
    kw = {
        "base": {"operacao_id": "61"},
        "chaves_coluna": [],
        "parametros": {"operacao_id": "61", "data_inicio": "2026-08-01", "data_fim": "2026-08-31"},
        "tipos_parametro": {"operacao_id": "texto", "data_inicio": "data", "data_fim": "data"},
        "dataset": "ds",
        "execucao_id": "e1",
    }
    with pytest.raises(ErroAplicacao) as e:
        publicados.selecionar_particoes(parts, {"data": pedido}, **kw)
    assert e.value.status == 400
    assert e.value.codigo == "filtro_particao_invalido"
    assert "'data'" in e.value.mensagem
    # The parameter, by its OWN name, is still checked against the run.
    assert publicados.selecionar_particoes(parts, {"data_inicio": "2026-08-01"}, **kw) == parts


# --- temporal type on the page, data failure x infrastructure, requested key, index schema -----


def _com_fuso(tmp_path) -> str:
    # 2001-06 and 2026-08 at -03; 2001-01 fell in daylight saving time (-02). The 2001 epoch has 15
    # digits and the 2026 one has 16: sorted as TEXT, 2001 would come AFTER 2026.
    return _parquet(
        tmp_path,
        "tz",
        "SELECT * FROM (VALUES "
        "(TIMESTAMPTZ '2026-08-06 12:17:23.5-03', 1), "
        "(TIMESTAMPTZ '2001-06-01 00:00:00-03', 2), "
        "(TIMESTAMPTZ '2001-01-01 00:00:00-03', 3)) t(registrado, n)",
    )


def test_timestamptz_sai_em_sao_paulo_com_offset_e_o_valor_copiado_casa(tmp_path):
    """TIMESTAMPTZ took down the whole page with a 500: the DuckDB-to-Python conversion requires
    `pytz`, which the product does not have. The cell comes out in America/Sao_Paulo with the offset
    OF THE INSTANT (2001-01-01 was daylight saving time), and the value copied from the response
    matches the `=` filter."""
    arq = _com_fuso(tmp_path)
    r = _consultar([arq], ordem=[("registrado", False)])
    assert r["colunas"][0] == {"nome": "registrado", "tipo": "TIMESTAMP WITH TIME ZONE"}
    assert r["linhas"] == [
        {"registrado": "2001-01-01T01:00:00-02:00", "n": 3},
        {"registrado": "2001-06-01T00:00:00-03:00", "n": 2},
        {"registrado": "2026-08-06T12:17:23.500000-03:00", "n": 1},
    ]
    for linha in r["linhas"]:
        f = _consultar([arq], filtros=[("registrado", "=", linha["registrado"])])
        assert [x["n"] for x in f["linhas"]] == [linha["n"]], linha
    # Without an offset, the value is read in the response timezone (Sao Paulo), not that of the machine answering.
    assert _consultar([arq], filtros=[("registrado", "=", "2026-08-06T12:17:23.5")])["total"] == 1
    assert (
        _consultar([arq], filtros=[("registrado", "=", "2026-08-06T15:17:23.5+00:00")])["total"]
        == 1
    )


def test_timestamptz_aninhado_nao_derruba_a_pagina(tmp_path):
    arq = _parquet(
        tmp_path, "tzs", "SELECT {'x': TIMESTAMPTZ '2026-08-06 12:17:23-03'} AS s, 1 AS n"
    )
    linha = _consultar([arq])["linhas"][0]
    assert linha["n"] == 1
    assert "2026-08-06 12:17:23-03" in linha["s"]


def test_time_e_timestamp_ns_copiados_da_resposta_casam(tmp_path):
    """TIME was compared as text (`12:17:23.500000` x `12:17:23.5`) and TIMESTAMP_NS came out cut at
    the microsecond: in both, the value copied from the row gave `total: 0`, with no error."""
    arq = _parquet(
        tmp_path,
        "tn",
        "SELECT * FROM (VALUES "
        "(TIME '12:17:23.5', TIMETZ '12:17:23.5-03', "
        "'2026-08-06 12:17:23.123456789'::TIMESTAMP_NS, 1), "
        "(TIME '12:17:23', TIMETZ '12:17:23-03', "
        "'2026-08-06 12:17:23.123456788'::TIMESTAMP_NS, 2)) t(h, htz, tns, n)",
    )
    linhas = {linha["n"]: linha for linha in _consultar([arq])["linhas"]}
    assert linhas[1]["tns"] == "2026-08-06T12:17:23.123456789"
    assert linhas[2]["tns"] == "2026-08-06T12:17:23.123456788"
    for n, linha in linhas.items():
        for coluna in ("h", "htz", "tns"):
            r = _consultar([arq], filtros=[(coluna, "=", linha[coluna])])
            assert [x["n"] for x in r["linhas"]] == [n], (coluna, linha[coluna])
    with pytest.raises(ErroAplicacao) as e:
        _consultar([arq], filtros=[("h", "=", "noon")])
    assert e.value.codigo == "filtro_invalido"


def test_falha_de_dado_no_agregado_nomeia_a_funcao_e_a_coluna_que_falhou(tmp_path):
    """The message used to blame the VALID summed column (and, with only `count_distinct`, said "sum"
    for the counted column). The function and the column that really fails to convert are named."""
    arq = _parquet(
        tmp_path,
        "fd",
        "SELECT 'abc' AS s, 'V1' AS orderid, CAST(1.50 AS DECIMAL(18,2)) AS v",
    )
    with pytest.raises(ErroAplicacao) as e:
        _consultar([arq], agregados=[("sum", "v"), ("count_distinct", "orderid"), ("sum", "S")])
    assert e.value.status == 400
    assert e.value.codigo == "agregacao_invalida"
    assert "sum 'S'" in e.value.mensagem
    assert "'v'" not in e.value.mensagem and "orderid" not in e.value.mensagem


def test_falta_de_memoria_no_agregado_e_503_nomeado_nunca_400_de_dado(tmp_path, monkeypatch):
    """Running out of memory is not "there is a value that is not a number": it is capacity, and a
    400 tells the client to fix a request that is correct. High-cardinality `count_distinct` within
    the `preview` budget is exactly where memory runs out."""
    arq = _parquet(
        tmp_path,
        "oom",
        "SELECT 'order_' || i || '_' || md5(i::VARCHAR) AS vid FROM range(4000000) t(i)",
    )
    original = publicados._abrir_duck

    def _apertado():
        con = original()
        con.execute("SET memory_limit = '40MB'")
        con.execute("SET temp_directory = ''")
        con.execute("SET threads = 2")
        return con

    monkeypatch.setattr(publicados, "_abrir_duck", _apertado)
    with pytest.raises(ErroAplicacao) as e:
        _consultar([arq], agregados=[("count_distinct", "vid")], take=1)
    assert e.value.status == 503
    assert e.value.codigo == "consulta_sem_memoria"
    assert "sum" not in e.value.mensagem


def test_chave_do_agregado_e_o_nome_pedido_nao_a_grafia_do_dataset(tmp_path):
    """Whoever asks for `count_distinct:orderid` reads `agregados['count_distinct']['orderid']`:
    returning it under `OrderId` (the file spelling) made the client miss the total."""
    arq = _parquet(
        tmp_path,
        "kp",
        "SELECT * FROM (VALUES (1, CAST(2.50 AS DECIMAL(10,2))), (2, CAST(1.00 AS DECIMAL(10,2)))) "
        "t(OrderId, Valor)",
    )
    r = _consultar([arq], agregados=[("count_distinct", "orderid"), ("sum", "valor")])
    assert r["agregados"] == {"count_distinct": {"orderid": "2"}, "sum": {"valor": "3.50"}}
    vazio = _consultar([], caminho_esquema=arq, agregados=[("count_distinct", "orderid")])
    assert vazio["agregados"] == {"count_distinct": {"orderid": "0"}}


def test_sem_arquivo_nenhum_valida_contra_as_colunas_do_indice():
    """An index with `particoes: []` (column partitioning + `permitir_vazio`): there is no parquet to
    read the schema from, and the index carries `colunas`. A requested column the dataset does not
    have is still a 400."""
    colunas = ["OrderId", "Valor"]
    r = _consultar(
        [],
        colunas_esquema=colunas,
        filtros=[("orderid", "=", "1")],
        ordem=[("valor", True)],
        agregados=[("count_distinct", "orderid"), ("sum", "Valor")],
    )
    assert r == {
        "colunas": [{"nome": "OrderId", "tipo": None}, {"nome": "Valor", "tipo": None}],
        "linhas": [],
        "total": 0,
        "agregados": {"count_distinct": {"orderid": "0"}, "sum": {"Valor": None}},
    }
    for kw in (
        {"filtros": [("doesnotexist", "=", "1")]},
        {"ordem": [("tb", False)]},
        {"agregados": [("sum", "nothing")]},
    ):
        with pytest.raises(ErroAplicacao) as e:
            _consultar([], colunas_esquema=colunas, **kw)
        assert e.value.status == 400
        assert e.value.codigo == "coluna_inexistente"


def test_sem_arquivo_e_sem_esquema_e_409_nunca_200_sem_schema():
    with pytest.raises(ErroAplicacao) as e:
        _consultar([], agregados=[("count_distinct", "orderid")])
    assert e.value.status == 409
    assert e.value.codigo == "esquema_indisponivel"


def _param(filtros, parametros, tipos):
    parts = [{"particao": {"operacao_id": "61"}, "caminho": "a", "linhas": 1}]
    return publicados.selecionar_particoes(
        parts,
        filtros,
        base={"operacao_id": "61"},
        chaves_coluna=[],
        parametros={"operacao_id": "61", **parametros},
        tipos_parametro={"operacao_id": "texto", **tipos},
        dataset="ds",
        execucao_id="e1",
    )


_AS_OF = ({"data_registro": "2026-08-06 12:17:23"}, {"data_registro": "as_of"})


@pytest.mark.parametrize(
    "pedido", ["2026-08-06 12:17:23", "2026-08-06T12:17:23", "2026-08-06T12:17:23.000000"]
)
def test_as_of_que_nao_particiona_casa_pelo_instante(pedido):
    assert len(_param({"data_registro": pedido}, *_AS_OF)) == 1


@pytest.mark.parametrize("pedido", ["2026-08-06T23:59:59", "2026-08-06", "2026-08-06T12:17"])
def test_as_of_de_outro_instante_do_mesmo_dia_e_404(pedido):
    """The as-of is the instant of the snapshot: two runs of the same day with different as-of
    values are different executions. Cutting at the day made `23:59:59` (and the bare day, which
    the resolver reads as the end of the day) read the 12:17:23 run with no error."""
    with pytest.raises(ErroAplicacao) as e:
        _param({"data_registro": pedido}, *_AS_OF)
    assert e.value.status == 404
    assert e.value.codigo == "publicado_ausente"
    assert "2026-08-06 12:17:23" in e.value.mensagem


def test_as_of_que_nao_e_instante_e_400():
    with pytest.raises(ErroAplicacao) as e:
        _param({"data_registro": "whenever"}, *_AS_OF)
    assert e.value.status == 400
    assert e.value.codigo == "filtro_particao_invalido"


def test_data_casa_pelo_dia_e_texto_e_verbatim():
    """`data` is a day by definition; a `texto` that looks like an instant (a stamp from the origin)
    does not lose the time - `texto_particao` cuts at the day because it serves a PATH SEGMENT, not
    this."""
    assert _param({"dia": "2026-08-06"}, {"dia": "2026-08-06"}, {"dia": "data"})
    assert _param({"corte": "2026-08-06 10:00"}, {"corte": "2026-08-06 10:00"}, {"corte": "texto"})
    with pytest.raises(ErroAplicacao) as e:
        _param({"corte": "2026-08-06"}, {"corte": "2026-08-06 10:00"}, {"corte": "texto"})
    assert e.value.status == 404
