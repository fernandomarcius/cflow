"""Validation of a function library definition (422 in the standard envelope).

Format: { "funcoes": [ {nome, args, corpo, retorno?, descricao?}, ... ] }.
Each function becomes a CREATE OR REPLACE MACRO (see `compilar.montar_ddl`).
"""

from __future__ import annotations

import re

from ..errors import ErroAplicacao

_IDENT = re.compile(r"^[a-zA-Z_]\w*$")
_RETORNOS = ("scalar", "struct")


def _erro(msg: str) -> None:
    raise ErroAplicacao(codigo="definicao_invalida", mensagem=msg, status=422)


def validar(d: dict) -> None:
    funcoes = d.get("funcoes")
    if not isinstance(funcoes, list) or not funcoes:
        _erro("A library needs at least one function.")
    nomes: list[str] = []
    for f in funcoes:
        if not isinstance(f, dict):
            _erro("Each function must be an object.")
        nome = f.get("nome")
        if not isinstance(nome, str) or not _IDENT.match(nome):
            _erro("Each function needs a valid name (letter/_ followed by letters, digits or _).")
        nomes.append(nome)

        args = f.get("args", [])
        if not isinstance(args, list):
            _erro(f"args of function '{nome}' must be a list of identifiers.")
        for a in args:
            if not isinstance(a, str) or not _IDENT.match(a):
                _erro(
                    f"Invalid argument in '{nome}': use identifiers "
                    "(letter/_ followed by letters, digits or _)."
                )

        corpo = f.get("corpo")
        if not isinstance(corpo, str) or not corpo.strip():
            _erro(f"Function '{nome}' needs a non-empty body (SQL expression).")

        retorno = f.get("retorno", "scalar")
        if retorno not in _RETORNOS:
            _erro(f"retorno of function '{nome}' must be 'scalar' or 'struct'.")

        if "descricao" in f and not isinstance(f["descricao"], str):
            _erro(f"descricao of function '{nome}' must be text.")

    if len(set(nomes)) != len(nomes):
        _erro("Function names must be unique within the library.")
