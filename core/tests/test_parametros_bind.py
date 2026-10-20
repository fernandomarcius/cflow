"""Safe parameter binding (:name -> driver placeholder, native bind). No database."""

from __future__ import annotations

from cflow_core.conexoes.parametros import (
    NUMERADO,
    PYFORMAT,
    QMARK,
    interpolar_literais,
    preparar_duckdb,
    refs_declaradas,
    vincular,
)


def test_sem_params_devolve_sql_intacto():
    sql = "SELECT * FROM t WHERE v = :x"
    assert vincular(sql, None, PYFORMAT) == (sql, [])
    assert vincular(sql, {}, PYFORMAT) == (sql, [])


def test_substitui_um_nome_por_estilo():
    assert vincular("v = :x", {"x": 7}, PYFORMAT) == ("v = %s", [7])
    assert vincular("v = :x", {"x": 7}, NUMERADO) == ("v = $1", [7])
    assert vincular("v = :x", {"x": 7}, QMARK) == ("v = ?", [7])


def test_pyformat_escapa_percent_literal():
    # A literal `%` (comment/LIKE) becomes `%%` so it does not collide with the params' `%s` in pytds.
    sql, vals = vincular("SELECT '9%' x FROM t WHERE a = :foo", {"foo": 1}, PYFORMAT)
    assert sql == "SELECT '9%%' x FROM t WHERE a = %s"
    assert vals == [1]


def test_qmark_e_numerado_nao_escapam_percent():
    # only PYFORMAT uses `%`; the other styles keep a literal `%` intact.
    assert vincular("x LIKE '%a' AND v = :y", {"y": 2}, QMARK) == ("x LIKE '%a' AND v = ?", [2])
    assert vincular("x LIKE '%a' AND v = :y", {"y": 2}, NUMERADO) == ("x LIKE '%a' AND v = $1", [2])


def test_multiplos_nomes_em_ordem():
    sql, vals = vincular("a = :op AND m = :mes", {"op": 43, "mes": "atual"}, NUMERADO)
    assert sql == "a = $1 AND m = $2"
    assert vals == [43, "atual"]


def test_mesmo_nome_repetido_gera_dois_binds():
    sql, vals = vincular(":x = a OR :x = b", {"x": 5}, NUMERADO)
    assert sql == "$1 = a OR $2 = b"
    assert vals == [5, 5]


def test_nome_nao_declarado_fica_intacto():
    # :y is not in params, so it stays literal; only :x is bound.
    sql, vals = vincular("a = :x AND b = :y", {"x": 1}, PYFORMAT)
    assert sql == "a = %s AND b = :y"
    assert vals == [1]


def test_cast_postgres_nao_e_confundido():
    # `::int` must not become a bind, even if there is a param named "int".
    sql, vals = vincular("SELECT x::int WHERE v = :v", {"v": 1, "int": 9}, NUMERADO)
    assert sql == "SELECT x::int WHERE v = $1"
    assert vals == [1]


def test_valor_de_injecao_vai_como_literal():
    # The value (even malicious SQL) NEVER enters the SQL text, only the bind.
    payload = "b' OR '1'='1; DROP TABLE target;--"
    sql, vals = vincular("WHERE v = :target", {"target": payload}, PYFORMAT)
    assert sql == "WHERE v = %s"
    assert vals == [payload]
    assert "DROP TABLE" not in sql and "OR '1'='1" not in sql


# ---- interpolar_literais (SQL-over-REST gateway, no native bind) ----


def test_interpolar_tipos():
    sql = "WHERE d >= :ini AND d < :fim AND op = :op AND f = :f AND n = :n"
    out = interpolar_literais(
        sql, {"ini": "2026-04-01", "fim": "2026-05-01", "op": 2, "f": 1.5, "n": None}
    )
    assert out == "WHERE d >= '2026-04-01' AND d < '2026-05-01' AND op = 2 AND f = 1.5 AND n = NULL"


def test_interpolar_escapa_aspas():
    out = interpolar_literais("WHERE nome = :n", {"n": "O'Brien'; DROP TABLE x;--"})
    # doubled single quotes: it stays a single literal (no injection)
    assert out == "WHERE nome = 'O''Brien''; DROP TABLE x;--'"


def test_interpolar_sem_params_e_cast_intacto():
    assert interpolar_literais("SELECT x::int", None) == "SELECT x::int"
    assert (
        interpolar_literais("SELECT x::int WHERE v = :v", {"v": 1, "int": 9})
        == "SELECT x::int WHERE v = 1"
    )


# ---- refs_declaradas / preparar_duckdb (sql/guided node SQL running on DuckDB) ----


def test_refs_declaradas_so_conta_o_declarado():
    sql = "SELECT * FROM t WHERE a = :unit AND b = :undeclared AND c = x::int"
    assert refs_declaradas(sql, {"unit", "int"}) == {"unit"}
    assert refs_declaradas(None, {"unit"}) == set()


def test_preparar_duckdb_vincula_por_qmark():
    sql, vals = preparar_duckdb('a."UNIT" = :unit', {"unit": "alpha"})
    assert sql == 'a."UNIT" = ?'
    assert vals == ["alpha"]


def test_preparar_duckdb_param_vazio_neutraliza_predicado():
    # "blank = everything": with no value the predicate is dropped (no NULL filter, which would match nothing).
    sql, vals = preparar_duckdb('WHERE a."UNIT" = :unit', {"unit": None})
    assert sql == "WHERE 1=1"
    assert vals == []


def test_preparar_duckdb_sem_params_intacto():
    sql = "SELECT * FROM t WHERE v = :x"
    assert preparar_duckdb(sql, None) == (sql, [])
    assert preparar_duckdb(sql, {}) == (sql, [])


def test_preparar_duckdb_aceita_grafia_dolar_do_sqlglot():
    """`gerar_sql` of the guided operation goes through sqlglot, which rewrites `:name` -> `$name`.
    Without recognizing both spellings, `$name` would arrive without a value ("Values were not provided")."""
    import sqlglot

    gerado = sqlglot.transpile(
        'SELECT * FROM u WHERE "UNIT" = :unit', read="duckdb", write="duckdb"
    )[0]
    assert "$unit" in gerado  # test premise (sqlglot behaviour)
    sql, vals = preparar_duckdb(gerado, {"unit": "alpha"})
    assert sql == 'SELECT * FROM u WHERE "UNIT" = ?'
    assert vals == ["alpha"]


def test_preparar_duckdb_nao_toca_dolar_posicional_nem_nao_declarado():
    sql, vals = preparar_duckdb("a = $1 AND b = $outro AND c = :x", {"x": 1})
    assert sql == "a = $1 AND b = $outro AND c = ?"
    assert vals == [1]


def test_preparar_duckdb_roda_no_duckdb():
    """End-to-end proof: the prepared SQL EXECUTES on DuckDB (which does not understand `:name`)."""
    import duckdb

    d = duckdb.connect()
    d.execute("CREATE TABLE t AS SELECT * FROM (VALUES (1,'alpha'),(2,'beta')) v(n, \"UNIT\")")
    sql, vals = preparar_duckdb('SELECT n FROM t WHERE "UNIT" = :unit', {"unit": "alpha"})
    assert d.execute(sql, vals).fetchall() == [(1,)]
    # no value: returns everything
    sql2, vals2 = preparar_duckdb('SELECT n FROM t WHERE "UNIT" = :unit', {"unit": None})
    assert d.execute(sql2, vals2).fetchall() == [(1,), (2,)]


def test_interrogacao_em_comentario_nao_vira_placeholder():
    """In QMARK style every `?` in the text is a positional placeholder, including one inside a
    comment. A `-- save_flag?value=` made DuckDB expect one more parameter than the caller passes,
    and the error surfaced as "Parameter argument/count mismatch" without saying where. Same problem
    the literal `%` already solved in PYFORMAT."""
    sql = "-- call: save_flag?value=&operator=\nSELECT * FROM t WHERE d <= :fim"
    out, valores = preparar_duckdb(sql, {"fim": "2026-07-01"})
    assert out.count("?") == 1  # only the bind's
    assert valores == ["2026-07-01"]


def test_interrogacao_em_comentario_de_bloco_tambem_sai():
    out, valores = preparar_duckdb("/* url?a=1 */ SELECT :x AS v", {"x": 1})
    assert out.count("?") == 1 and valores == [1]


def test_interrogacao_dentro_de_STRING_e_preservada():
    """A `?` in a string literal is not a driver placeholder, and removing it would change the DATA."""
    out, valores = preparar_duckdb("SELECT 'has ? here' AS t, :x AS v", {"x": 1})
    assert "'has ? here'" in out
    assert out.count("?") == 2 and valores == [1]


def test_parametro_CITADO_em_comentario_nao_consome_valor():
    """`:name` inside a comment is not a parameter: the driver does not see it. Replacing it puts in
    a placeholder DuckDB ignores and passes one value too many, and the error surfaced as
    "Parameter argument/count mismatch" without pointing at the line."""
    sql = "-- cut off at :fim\nSELECT * FROM t WHERE a <= :fim AND b <= :fim"
    out, valores = preparar_duckdb(sql, {"fim": "2026-07-01"})
    assert out.count("?") == 2  # the two real uses, not the three textual ones
    assert len(valores) == 2
    assert ":fim" in out.splitlines()[0]  # the comment stays readable


def test_parametro_em_comentario_de_bloco_tambem_nao_conta():
    out, valores = preparar_duckdb("/* uses :x here */ SELECT :x AS v", {"x": 1})
    assert out.count("?") == 1 and valores == [1]
