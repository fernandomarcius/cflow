"""Compilation of function libraries into DuckDB MACRO DDL.

Concatenates the functions of one or more libraries loaded together into
`CREATE OR REPLACE MACRO name(args) AS (body);`. Detects name collisions between libraries
(the same MACRO defined in two -> 409). The same DDL runs in preview and in execution.
"""

from __future__ import annotations

from ..errors import ErroAplicacao


def montar_ddl(definicoes: list[dict]) -> str:
    """Build the DDL of all functions in `definicoes` (a list of library defs, each with
    `funcoes`). Joined with `\\n`. Raises 409 if the same MACRO appears in more than one library
    loaded together. Returns an empty string if there is no definition."""
    vistos: set[str] = set()
    linhas: list[str] = []
    for d in definicoes:
        for f in d.get("funcoes", []):
            nome = f["nome"]
            if nome in vistos:
                msg = f"Function '{nome}' is defined in more than one library loaded together."
                raise ErroAplicacao(codigo="macro_colisao", mensagem=msg, status=409)
            vistos.add(nome)
            args = ", ".join(f.get("args", []))
            corpo = f["corpo"]
            # return 'struct': the body already returns struct_pack(...); the scalar MACRO emits the same.
            linhas.append(f"CREATE OR REPLACE MACRO {nome}({args}) AS ({corpo});")
    return "\n".join(linhas)
