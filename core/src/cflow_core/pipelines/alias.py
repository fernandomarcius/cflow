"""Node label as a SQL table ALIAS (in addition to the stable id).

The id remains the canonical reference (edges, snapshots and `{id}` in SQL, so renaming never
breaks anything). The alias is a convenience VIEW named after the node label, created both in
preview and in execution (same rule, so preview == execution): `SELECT * FROM Orders` and
`SELECT * FROM n7` become equivalent.

A node has NO alias (only the id works) when its label: is empty; collides (case-insensitive)
with any node id; uses the reserved prefix `fonte_`; or is repeated across nodes (ambiguous).
"""

from __future__ import annotations

from collections import Counter


def aliases_de_rotulos(definicao: dict) -> dict[str, str]:
    """{node id: label usable as alias}. Nodes without a valid alias are left out."""
    nos = definicao.get("nos", [])
    ids = {str(n.get("id", "")).lower() for n in nos}
    rotulos = {str(n.get("id")): str(n.get("rotulo") or "").strip() for n in nos}
    contagem = Counter(r.lower() for r in rotulos.values() if r)
    out: dict[str, str] = {}
    for nid, rotulo in rotulos.items():
        rl = rotulo.lower()
        if not rotulo or rl in ids or rl.startswith("fonte_") or contagem[rl] > 1:
            continue
        out[nid] = rotulo
    return out


def _q(identificador: str) -> str:
    return '"' + identificador.replace('"', '""') + '"'


def criar_alias(duck, no_id: str, alias: str) -> None:
    """VIEW `alias` -> the node table/view. Idempotent (OR REPLACE)."""
    duck.execute(f"CREATE OR REPLACE VIEW {_q(alias)} AS SELECT * FROM {_q(no_id)}")


def remover_alias(duck, alias: str) -> None:
    duck.execute(f"DROP VIEW IF EXISTS {_q(alias)}")
