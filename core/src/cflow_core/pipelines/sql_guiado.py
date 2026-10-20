"""Guided operations -> SQL (validated/normalized by sqlglot, DuckDB dialect).

Generates the displayable SQL for operation nodes (filter/group/join). The result follows the
same preview/execution invariant. `entradas` are upstream node ids (they become table names),
in edge order: filter/group use 1; a keyed join uses 2; a join in union mode
(`op.modo == "uniao"`) stacks 2 OR MORE inputs with UNION [ALL] BY NAME (aligns columns by
name; `op.distinto` removes duplicates).
"""

from __future__ import annotations

import sqlglot

from ..errors import ErroAplicacao


def gerar_sql(op: dict, *, entradas: list[str]) -> str:
    tipo = op.get("tipo")
    if not entradas:
        raise ErroAplicacao(
            codigo="op_sem_entrada", mensagem="Operation has no connected input node.", status=400
        )
    entrada = entradas[0]
    if tipo == "filtrar":
        sql = f"SELECT * FROM {entrada} WHERE {op['condicao']}"
    elif tipo == "agrupar":
        chaves = ", ".join(op["chaves"])
        aggs = ", ".join(op["agregacoes"])  # e.g. "count(*) AS qtd"
        sql = f"SELECT {chaves}, {aggs} FROM {entrada} GROUP BY {chaves}"
    elif tipo == "juntar":
        if op.get("modo") == "uniao":
            if len(entradas) < 2:
                raise ErroAplicacao(
                    codigo="op_invalida", mensagem="Union requires at least two inputs.", status=400
                )
            operador = "UNION BY NAME" if op.get("distinto") else "UNION ALL BY NAME"
            sql = f" {operador} ".join(f"SELECT * FROM {e}" for e in entradas)
        else:
            if len(entradas) < 2:
                raise ErroAplicacao(
                    codigo="op_invalida", mensagem="Join requires two inputs.", status=400
                )
            sql = f"SELECT * FROM {entradas[0]} JOIN {entradas[1]} ON {op['condicao']}"
    else:
        raise ErroAplicacao(
            codigo="op_nao_suportada", mensagem=f"Operation '{tipo}' is not supported.", status=400
        )

    try:
        return sqlglot.transpile(sql, read="duckdb", write="duckdb")[0]
    except Exception as exc:  # noqa: BLE001
        raise ErroAplicacao(
            codigo="sql_invalido", mensagem="Generated SQL is invalid.", status=400
        ) from exc


def validar_uniao(duck, op: dict, entradas: list[str]) -> None:
    """Pre-validate the UNION with a clear error BEFORE DuckDB (which would silently fill a
    missing column with NULL). No-op if the op is not a join in union mode.
    `duck` is the preview/execution connection with the predecessors' tables/views already
    created. Rule: all inputs must have the SAME set of columns (alignment is by name)."""
    if op.get("tipo") != "juntar" or op.get("modo") != "uniao":
        return
    cols: dict[str, list[str]] = {}
    for e in entradas:
        cols[e] = [str(r[0]) for r in duck.execute(f'DESCRIBE "{e}"').fetchall()]
    todas: set[str] = set().union(*(set(c) for c in cols.values())) if cols else set()
    faltas = {e: sorted(todas - set(c)) for e, c in cols.items() if todas - set(c)}
    if faltas:
        detalhe = "; ".join(f"'{e}' is missing: {', '.join(f)}" for e, f in faltas.items())
        raise ErroAplicacao(
            codigo="uniao_colunas_diferentes",
            mensagem=f"The union inputs do not have the same columns: {detalhe}.",
            status=400,
        )
