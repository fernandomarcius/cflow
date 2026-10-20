"""Node label as a table alias in SQL: mapping rules (pure logic)."""

from __future__ import annotations

import duckdb

from cflow_core.pipelines.alias import aliases_de_rotulos, criar_alias, remover_alias


def _def(*nos):
    return {"nos": [{"id": i, "rotulo": r} for i, r in nos]}


def test_rotulo_vira_alias():
    assert aliases_de_rotulos(_def(("n7", "Orders"))) == {"n7": "Orders"}


def test_sem_rotulo_sem_alias():
    assert aliases_de_rotulos(_def(("n1", ""), ("n2", None))) == {}


def test_rotulo_igual_a_id_de_no_nao_vira_alias():
    # "n2" (even with different case) collides with the other node id: ambiguous, left out
    assert aliases_de_rotulos(_def(("n1", "N2"), ("n2", "ok"))) == {"n2": "ok"}


def test_rotulo_repetido_e_ambiguo():
    assert aliases_de_rotulos(_def(("n1", "Sales"), ("n2", "sales"), ("n3", "Other"))) == {
        "n3": "Other"
    }


def test_prefixo_fonte_reservado():
    assert aliases_de_rotulos(_def(("n1", "fonte_n1"))) == {}


def test_criar_e_remover_alias_com_aspas_e_espaco():
    duck = duckdb.connect()
    duck.execute('CREATE TABLE "n7" AS SELECT 1 AS a')
    criar_alias(duck, "n7", 'Source "Orders" 2024')
    assert duck.execute('SELECT a FROM "Source ""Orders"" 2024"').fetchone()[0] == 1
    remover_alias(duck, 'Source "Orders" 2024')
    criar_alias(duck, "n7", "Orders")
    assert duck.execute("SELECT a FROM Orders").fetchone()[0] == 1  # unquoted, case-insensitive
