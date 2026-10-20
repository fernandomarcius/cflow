"""DuckDB slot: one instance per node (default) or shared by the process.

The tests pin down what makes the switch safe: isolation between nodes, a MACRO that does not
leak, a schema that is not left behind, and an instance that outlives the node."""

from __future__ import annotations

import asyncio

import duckdb
import pytest

from cflow_core.config import get_settings
from cflow_core.execucao import instancia


@pytest.fixture(autouse=True)
def _limpar():
    yield
    instancia.descartar()
    get_settings.cache_clear()


def _compartilhado(monkeypatch, ligado: bool) -> None:
    monkeypatch.setattr(get_settings(), "duckdb_compartilhado", ligado)


def test_por_no_fecha_a_instancia(monkeypatch):
    """The default: the instance belongs to the node and dies with it."""
    _compartilhado(monkeypatch, False)
    slot = instancia.abrir()
    slot.con.execute("CREATE TABLE t AS SELECT 1")
    slot.fechar()
    with pytest.raises(duckdb.Error):
        slot.con.execute("SELECT 1")


def test_compartilhada_isola_os_nos_pelo_schema(monkeypatch):
    """Two nodes create a table with the SAME name without clashing, as the unqualified names in
    the node SQL require."""
    _compartilhado(monkeypatch, True)
    a, b = instancia.abrir(), instancia.abrir()
    a.con.execute("CREATE TABLE t AS SELECT 1 AS x")
    b.con.execute("CREATE TABLE t AS SELECT 2 AS x")
    assert a.con.execute("SELECT x FROM t").fetchone() == (1,)
    assert b.con.execute("SELECT x FROM t").fetchone() == (2,)
    a.fechar()
    b.fechar()


def test_compartilhada_nao_deixa_macro_vazar(monkeypatch):
    """The function library is PINNED per execution: two executions with different versions of the
    same MACRO in one process must not see each other."""
    _compartilhado(monkeypatch, True)
    a, b = instancia.abrir(), instancia.abrir()
    a.con.execute("CREATE MACRO dobro(x) AS x * 2")
    assert a.con.execute("SELECT dobro(2)").fetchone() == (4,)
    with pytest.raises(duckdb.Error):
        b.con.execute("SELECT dobro(2)")
    a.fechar()
    b.fechar()


def test_compartilhada_fechar_apaga_o_schema_do_no(monkeypatch):
    """Without the DROP, each node would leave its schema in the catalog, growing for as long as
    the worker lives."""
    _compartilhado(monkeypatch, True)
    slot = instancia.abrir()
    slot.con.execute("CREATE TABLE t AS SELECT 1")
    nome = slot._schema
    slot.fechar()
    outro = instancia.abrir()
    assert outro.con.execute(
        "SELECT count(*) FROM information_schema.schemata WHERE schema_name = ?", [nome]
    ).fetchone() == (0,)
    outro.fechar()


def test_compartilhada_sobrevive_ao_fim_do_no(monkeypatch):
    """The gain depends on this: closing the node must NOT cost the instance, which is what we
    want to reuse."""
    _compartilhado(monkeypatch, True)
    primeiro = instancia.abrir()
    primeiro.fechar()
    segundo = instancia.abrir()
    assert segundo.con.execute("SELECT 1").fetchone() == (1,)
    segundo.fechar()


def test_descartar_zera_a_instancia(monkeypatch):
    _compartilhado(monkeypatch, True)
    slot = instancia.abrir()
    slot.fechar()
    anterior = instancia._INSTANCIA
    instancia.descartar()
    assert instancia._INSTANCIA is None
    with pytest.raises(duckdb.Error):
        anterior.execute("SELECT 1")


def test_cursor_filho_enxerga_a_tabela_do_no(monkeypatch):
    """Partitioned publishing opens one cursor per scope. A cursor is another CONNECTION of the
    same instance and starts with the default `search_path`; with the shared instance it would not
    see the node's table, and the COPY would die with `Table with name ... does not exist!`."""
    _compartilhado(monkeypatch, True)
    slot = instancia.abrir()
    try:
        slot.con.execute("CREATE TABLE t AS SELECT 1 AS a")
        with pytest.raises(duckdb.Error):
            slot.con.cursor().execute("SELECT * FROM t")  # raw cursor: default schema
        filho = instancia.cursor(slot.con)
        try:
            assert filho.execute("SELECT a FROM t").fetchone() == (1,)
        finally:
            filho.close()
    finally:
        slot.fechar()


def test_cursor_filho_e_no_op_com_instancia_propria(monkeypatch):
    """With the node's own instance the `search_path` is already the default: the cursor is unchanged."""
    _compartilhado(monkeypatch, False)
    slot = instancia.abrir()
    try:
        slot.con.execute("CREATE TABLE t AS SELECT 2 AS a")
        filho = instancia.cursor(slot.con)
        try:
            assert filho.execute("SELECT a FROM t").fetchone() == (2,)
        finally:
            filho.close()
    finally:
        slot.fechar()


@pytest.mark.asyncio
async def test_teto_de_slots_abertos_no_processo(monkeypatch):
    """The `duck.orcamento` budget divides memory by `worker_max_atividades`, but the cap on nodes
    in flight is per EXECUTION, so many instances could run at once against a budget computed for
    a few. `entrar` is what makes that invariant actually hold."""
    monkeypatch.setenv("CFLOW_WORKER_MAX_ATIVIDADES", "2")
    get_settings.cache_clear()
    instancia._VAGAS.clear()
    _compartilhado(monkeypatch, False)

    a, b = await instancia.entrar(), await instancia.entrar()
    terceira = asyncio.ensure_future(instancia.entrar())
    await asyncio.sleep(0)
    assert not terceira.done(), "the third must wait: the cap is 2"

    slot = instancia.abrir("execucao", a)
    slot.fechar()  # returns the slot from inside the node thread (here, directly)
    # `wait_for`, not `sleep(0)`: the release goes through `call_soon_threadsafe`, so it takes two
    # loop cycles: one for the `release`, another to wake the waiter.
    (await asyncio.wait_for(terceira, timeout=2)).sair()
    b.sair()


@pytest.mark.asyncio
async def test_vaga_devolvida_de_outra_thread(monkeypatch):
    """`asyncio.Semaphore.release` is not thread-safe, and the node thread is what closes the slot."""
    monkeypatch.setenv("CFLOW_WORKER_MAX_ATIVIDADES", "1")
    get_settings.cache_clear()
    instancia._VAGAS.clear()
    _compartilhado(monkeypatch, False)

    vaga = await instancia.entrar()
    slot = instancia.abrir("execucao", vaga)
    await asyncio.to_thread(slot.fechar)
    await asyncio.sleep(0)
    segunda = await asyncio.wait_for(instancia.entrar(), timeout=2)
    segunda.sair()


@pytest.mark.asyncio
async def test_sair_e_idempotente(monkeypatch):
    """The error path of `_abrir` returns the slot, and the slot's `fechar` would return it again:
    releasing twice would silently raise the cap."""
    monkeypatch.setenv("CFLOW_WORKER_MAX_ATIVIDADES", "1")
    get_settings.cache_clear()
    instancia._VAGAS.clear()

    vaga = await instancia.entrar()
    vaga.sair()
    vaga.sair()
    await asyncio.sleep(0)
    primeira = await asyncio.wait_for(instancia.entrar(), timeout=2)
    segunda = asyncio.ensure_future(instancia.entrar())
    await asyncio.sleep(0)
    assert not segunda.done(), "two releases must not become two slots"
    primeira.sair()
    await asyncio.sleep(0)
    (await segunda).sair()
