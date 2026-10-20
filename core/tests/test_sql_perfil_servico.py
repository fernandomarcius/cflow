"""SQL that compares the profile with `servico` compares as TEXT.

The `servico` label only exists in the `perfil` enum from migration 0040 on. `perfil = 'servico'`
makes Postgres read the literal as an enum value and refuse the whole query ("invalid input value
for enum perfil") on a database that does not have it yet: the list endpoints would answer 500
for EVERY user, not only for the service account, if the new image started before the migration.
With `perfil::text = 'servico'` the comparison is textual, and the code runs before or after the
migration.
"""

from __future__ import annotations

import pathlib
import re

RAIZ = pathlib.Path(__file__).resolve().parents[2]
PASTAS = ("core/src", "api/src")
# The enum compared directly with the literal; `perfil::text = 'servico'` does not match (the `::text` splits it).
PADRAO = re.compile(r"perfil\s*(?:=|<>|!=|IN\s*\()\s*'servico'", re.IGNORECASE)


def test_o_padrao_pega_a_forma_que_derrubou_a_listagem():
    assert PADRAO.search("SELECT perfil = 'servico' FROM usuarios WHERE id = :id")
    assert PADRAO.search("AND u.ativo AND u.perfil = 'servico'")
    assert PADRAO.search("WHERE perfil IN ('servico', 'admin')")
    assert not PADRAO.search("SELECT perfil::text = 'servico' FROM usuarios")


def test_nenhum_sql_compara_o_enum_perfil_com_o_literal_servico():
    achados = []
    for pasta in PASTAS:
        for arq in sorted((RAIZ / pasta).rglob("*.py")):
            for n, linha in enumerate(arq.read_text(encoding="utf-8").splitlines(), 1):
                if PADRAO.search(linha):
                    achados.append(f"{arq.relative_to(RAIZ)}:{n}: {linha.strip()}")
    assert not achados, "compare as text (perfil::text = 'servico'):\n" + "\n".join(achados)
