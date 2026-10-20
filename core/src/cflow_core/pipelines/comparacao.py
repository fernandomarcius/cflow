"""Comparison node (diff by key) -> DuckDB SQL.

Compares two inputs (left x right) by a key; classifies each key as
`so_esquerda | so_direita | divergente | igual` (equal omitted by default). It is the
**equivalence gate** (e.g. validating a rebuilt table against a reference extract). Generic:
works for any pair of tables sharing the same key. Content equality uses the hash of the whole
row (`hash(to_json(t))`), so any diverging column marks the key as `divergente`.
"""

from __future__ import annotations

from ..errors import ErroAplicacao


def _ident(c: str) -> str:
    """Quoted SQL identifier (escapes inner quotes)."""
    return '"' + str(c).replace('"', '""') + '"'


def gerar_sql_comparacao(
    esquerda: str, direita: str, chave: list[str], *, incluir_iguais: bool = False
) -> str:
    if not chave or not isinstance(chave, list):
        raise ErroAplicacao(
            codigo="comparacao_invalida",
            mensagem="Comparison requires 'chave' (a list of columns).",
            status=400,
        )
    ks = ", ".join(_ident(c) for c in chave)
    base = (
        f"WITH _e AS (SELECT {ks}, hash(to_json(t)) AS _h FROM {_ident(esquerda)} t), "
        f"_d AS (SELECT {ks}, hash(to_json(t)) AS _h FROM {_ident(direita)} t) "
        f"SELECT {ks}, CASE "
        "WHEN _d._h IS NULL THEN 'so_esquerda' "
        "WHEN _e._h IS NULL THEN 'so_direita' "
        "WHEN _e._h IS DISTINCT FROM _d._h THEN 'divergente' "
        "ELSE 'igual' END AS _status "
        f"FROM _e FULL OUTER JOIN _d USING ({ks})"
    )
    if incluir_iguais:
        return base
    return f"SELECT * FROM ({base}) WHERE _status <> 'igual'"
