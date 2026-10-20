"""Safe binding of runtime parameters into a node's SQL.

A node's SQL references declared parameters as `:nome`. Here every **declared** `:nome` is replaced
by the driver placeholder and the values are returned in order, for *native binding*: the value is
NEVER concatenated into the SQL (injection-safe). Undeclared names are left untouched (e.g. the
Postgres `::type` cast, or an `:x` that is not a parameter).

It applies to both paths: the **source** SQL (sent to the connector driver) and the SQL that runs in
**DuckDB** (`sql`/`guiada` nodes); see `preparar_duckdb`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# `:nome` NOT preceded by `:` (avoids matching the Postgres `::cast`).
_PADRAO = re.compile(r"(?<!:):([a-zA-Z_]\w*)")

# `$nome` (not positional `$1`): sqlglot, when normalising the SQL of a guided operation in the
# DuckDB dialect, rewrites `:nome` as `$nome`. Both spellings must be recognised.
_PADRAO_DOLAR = re.compile(r"(?<![\w$])\$([a-zA-Z_]\w*)")

# Column reference in a predicate (word/qualified, with quotes/brackets): no spaces.
_LADO = r'[\w."\[\]]+'
_OPS = r"(?:=|<>|!=|>=|<=|>|<)"


def neutralizar_predicados_vazios(sql: str, vazios: set[str]) -> str:
    """ "Blank = everything": removes (turns into `1=1`) the PREDICATES that compare against an EMPTY
    parameter, for ANY SQL, by textual substitution without regenerating the SQL (the rest of the
    query is untouched).

    Covers `col OP :p`, `:p OP col`, `col [NOT] LIKE :p`, `col [NOT] IN (:p)` and
    `col BETWEEN :a AND :b` (when a or b is empty). Occurrences of the parameter OUTSIDE a predicate
    (e.g. `:p AS column` in the SELECT) stay intact and bind NULL. A parameter inside a complex
    expression with spaces in the left operand is not covered (rare in runtime filters)."""
    if not vazios:
        return sql
    out = sql
    for nome in vazios:
        p = re.escape(nome)
        out = re.sub(rf"{_LADO}\s*{_OPS}\s*(?<!:):{p}\b", "1=1", out)
        out = re.sub(rf"(?<!:):{p}\b\s*{_OPS}\s*{_LADO}", "1=1", out)
        out = re.sub(rf"{_LADO}\s+(?:NOT\s+)?LIKE\s*(?<!:):{p}\b", "1=1", out, flags=re.IGNORECASE)
        out = re.sub(
            rf"{_LADO}\s+(?:NOT\s+)?IN\s*\(\s*(?<!:):{p}\s*\)", "1=1", out, flags=re.IGNORECASE
        )

    def _betw(m: re.Match) -> str:
        return "1=1" if (m.group(1) in vazios or m.group(2) in vazios) else m.group(0)

    out = re.sub(
        rf"{_LADO}\s+BETWEEN\s+(?<!:):(\w+)\s+AND\s+(?<!:):(\w+)", _betw, out, flags=re.IGNORECASE
    )
    return out


# Placeholder styles per driver.
QMARK = "qmark"  # duckdb / sqlite → ?
NUMERADO = "numerado"  # asyncpg (postgres) → $1, $2, ...
PYFORMAT = "pyformat"  # pytds (sqlserver) → %s


def _fatiar_fora_de_comentario(sql: str) -> list[tuple[str, bool]]:
    """Splits the SQL into `(text, is_code)` chunks. Line and block comments get
    `is_code=False`; a string literal counts as code, because the driver does not interpret it but
    its content is DATA and must not be altered."""
    fora: list[tuple[str, bool]] = []
    i, n = 0, len(sql)
    atual: list[str] = []

    def _fecha() -> None:
        if atual:
            fora.append(("".join(atual), True))
            atual.clear()

    while i < n:
        c = sql[i]
        if c in "'\"":
            fim = sql.find(c, i + 1)
            fim = n if fim == -1 else fim + 1
            atual.append(sql[i:fim])
            i = fim
        elif sql.startswith("--", i):
            _fecha()
            fim = sql.find("\n", i)
            fim = n if fim == -1 else fim
            fora.append((sql[i:fim], False))
            i = fim
        elif sql.startswith("/*", i):
            _fecha()
            fim = sql.find("*/", i + 2)
            fim = n if fim == -1 else fim + 2
            fora.append((sql[i:fim], False))
            i = fim
        else:
            atual.append(c)
            i += 1
    _fecha()
    return fora


def _sem_interrogacao_em_comentario(sql: str) -> str:
    """Removes `?` from inside SQL comments (see `_fatiar_fora_de_comentario`)."""
    return "".join(
        t if codigo else t.replace("?", "") for t, codigo in _fatiar_fora_de_comentario(sql)
    )


def vincular(sql: str, params: dict | None, estilo: str) -> tuple[str, list]:
    """Rewrites `:nome` (present in params) to the driver placeholder; returns (sql, values_in_order).

    Without params, returns the SQL untouched and an empty list (legacy behaviour preserved).
    Names not present in params stay literal (the caller validates required ones at the edge).
    """
    if not params:
        return sql, []
    valores: list = []
    contador = {"i": 0}

    # PYFORMAT (pytds): the driver does `%`-style interpolation. Any literal `%` in the SQL (e.g. in
    # a comment or LIKE) must become `%%`, otherwise it collides with the parameters' `%s` and blows up
    # ("unsupported format character"). Escape BEFORE inserting the `%s` (which keep a single `%`).
    if estilo == PYFORMAT:
        sql = sql.replace("%", "%%")
    if estilo == QMARK:
        sql = _sem_interrogacao_em_comentario(sql)

    # `:nome` INSIDE a comment is not a parameter: the driver does not see it, but naive
    # substitution puts a placeholder there and passes one value too many (e.g. 7 real uses plus 1
    # mention in a comment gives 8 values for 7 placeholders, and DuckDB answers
    # "Parameter argument/count mismatch" without pointing at the line).
    pedacos = _fatiar_fora_de_comentario(sql)

    def _troca(m: re.Match) -> str:
        nome = m.group(1)
        if nome not in params:
            return m.group(0)  # not a declared parameter: leave it intact
        valores.append(params[nome])
        contador["i"] += 1
        if estilo == QMARK:
            return "?"
        if estilo == NUMERADO:
            return f"${contador['i']}"
        return "%s"  # PYFORMAT

    return (
        "".join(_PADRAO.sub(_troca, t) if codigo else t for t, codigo in pedacos),
        valores,
    )


def refs_declaradas(sql: str | None, nomes: Iterable[str]) -> set[str]:
    """DECLARED names that this SQL references (`:nome` or `$nome`); ignores `::cast`, `$1` and
    undeclared `:x`."""
    declarados = set(nomes)
    texto = sql or ""
    achados = {m.group(1) for m in _PADRAO.finditer(texto)}
    achados |= {m.group(1) for m in _PADRAO_DOLAR.finditer(texto)}
    return achados & declarados


def _dolar_para_dois_pontos(sql: str, nomes: set[str]) -> str:
    """`$nome` -> `:nome` (DECLARED names only): unifies the spelling before neutralising/binding."""
    return _PADRAO_DOLAR.sub(lambda m: f":{m.group(1)}" if m.group(1) in nomes else m.group(0), sql)


def preparar_duckdb(sql: str, params: dict | None) -> tuple[str, list]:
    """SQL of a `sql`/`guiada` node ready for DuckDB: (sql_with_?, values_in_order).

    DuckDB does not understand `:nome` (syntax error) and a `$nome` without a value blows up with
    "Values were not provided". Three steps, the SAME as for the source (invariant: the same SQL runs
    in preview and in execution):
    1. unify the spelling (`$nome` -> `:nome`);
    2. "blank = everything": a predicate comparing against an empty parameter becomes `1=1`;
    3. native binding: each remaining `:nome` becomes `?` and the value goes outside the SQL
       (injection-safe).

    `params` must contain EVERY declared parameter (no value -> None); see
    `cflow_core.pipelines.definicao.completar_declarados`.
    """
    if not params:
        return sql, []
    sql = _dolar_para_dois_pontos(sql, set(params))
    sql = neutralizar_predicados_vazios(sql, {k for k, v in params.items() if v is None})
    return vincular(sql, params, QMARK)


def interpolar_literais(sql: str, params: dict | None) -> str:
    """Replaces `:nome` with escaped SQL LITERALS. Use ONLY with gateways that receive the SQL as a
    STRING (no native binding, e.g. a SQL-over-REST gateway). Strings get doubled single quotes;
    numbers become text; None becomes NULL. Undeclared names stay intact (`::cast` preserved).
    The values here are derived parameters (dates/ids), not free-form operator text."""
    if not params:
        return sql

    def _lit(v) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, (int, float)):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"

    def _troca(m: re.Match) -> str:
        nome = m.group(1)
        return _lit(params[nome]) if nome in params else m.group(0)

    return _PADRAO.sub(_troca, sql)
