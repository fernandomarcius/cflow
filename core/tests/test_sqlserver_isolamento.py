"""Isolation of the SQL Server read SESSION (`config.isolamento`).

It exists because the source database receives writes while the extraction scans the table. With
`(nolock)` the scan follows physical order and drops rows that moved pages: the SAME extraction
(same SQL, same as-of) can return different row counts on the same day. Snapshot gives a picture of
the instant of the query without blocking the writer, and without `(nolock)`, which defeats the
isolation.
"""

from __future__ import annotations

import pytest

from cflow_core.conexoes.conectores import sqlserver
from cflow_core.errors import ErroAplicacao


class CursorFalso:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args):
        self._conn.executados.append(sql)

    def fetchone(self):
        return self._conn.estado_linha


class ConexaoFalsa:
    def __init__(self, estado_linha=None):
        self.executados: list[str] = []
        self.estado_linha = estado_linha
        self.fechada = False

    def cursor(self):
        return CursorFalso(self)

    def close(self):
        self.fechada = True


def test_sem_isolamento_na_config_a_sessao_fica_como_sempre():
    conn = ConexaoFalsa()
    sqlserver._preparar_sessao(conn, {"base": "cflow"}, True)
    assert conn.executados == []


@pytest.mark.parametrize("estado", [1, 2])
def test_snapshot_confere_o_banco_e_poe_a_sessao(estado):
    """`snapshot_isolation_state` 1 = ON, 2 = in transition (already usable for reading)."""
    conn = ConexaoFalsa(estado_linha={"snapshot_isolation_state": estado})
    sqlserver._preparar_sessao(conn, {"base": "cflow", "isolamento": "snapshot"}, True)
    assert conn.executados == [
        sqlserver._SQL_ESTADO_SNAPSHOT,
        "SET TRANSACTION ISOLATION LEVEL SNAPSHOT",
    ]


def test_linha_em_tupla_tambem_e_lida():
    """The columnar extraction connects with `as_dict=False`: the check cannot depend on a dict."""
    conn = ConexaoFalsa(estado_linha=(1,))
    sqlserver._preparar_sessao(conn, {"base": "cflow", "isolamento": "SNAPSHOT "}, True)
    assert conn.executados[-1] == "SET TRANSACTION ISOLATION LEVEL SNAPSHOT"


@pytest.mark.parametrize("estado", [0, None])
def test_banco_sem_allow_snapshot_isolation_falha_ANTES_da_consulta(estado):
    """Without the check, a database lacking the option only breaks on the first SELECT, with the raw
    3952 error, in the middle of a multi-minute extraction, and the message does not say what to do."""
    linha = None if estado is None else {"snapshot_isolation_state": estado}
    conn = ConexaoFalsa(estado_linha=linha)
    with pytest.raises(ErroAplicacao) as e:
        sqlserver._preparar_sessao(conn, {"base": "source_db", "isolamento": "snapshot"}, True)
    assert e.value.codigo == "isolamento_snapshot_indisponivel"
    assert "source_db" in e.value.mensagem
    assert "ALLOW_SNAPSHOT_ISOLATION" in e.value.mensagem
    assert "SET TRANSACTION ISOLATION LEVEL SNAPSHOT" not in conn.executados


def test_escrita_nao_entra_em_snapshot():
    """`autocommit=False` is the write path: there snapshot trades waiting for a conflict error
    (3960), which is a different decision, and writes do not suffer from the defect this isolation
    fixes."""
    conn = ConexaoFalsa(estado_linha={"snapshot_isolation_state": 1})
    sqlserver._preparar_sessao(conn, {"base": "cflow", "isolamento": "snapshot"}, False)
    assert conn.executados == []


def test_isolamento_fora_do_vocabulario_e_recusado():
    """Silently accepted, a typo would leave the read dirty while believing it is protected."""
    conn = ConexaoFalsa(estado_linha={"snapshot_isolation_state": 1})
    with pytest.raises(ErroAplicacao) as e:
        sqlserver._preparar_sessao(conn, {"base": "cflow", "isolamento": "serializable"}, True)
    assert e.value.codigo == "isolamento_invalido"
    assert conn.executados == []


def test_conectar_fecha_a_conexao_quando_o_isolamento_falha(monkeypatch):
    """An opened and abandoned connection would hold a server session on every attempt."""
    conn = ConexaoFalsa(estado_linha={"snapshot_isolation_state": 0})
    monkeypatch.setitem(
        __import__("sys").modules, "pytds", type("M", (), {"connect": lambda **kw: conn})
    )
    with pytest.raises(ErroAplicacao):
        sqlserver._conectar({"base": "cflow", "host": "h", "isolamento": "snapshot"}, "x")
    assert conn.fechada
