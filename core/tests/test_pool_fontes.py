"""Unit tests for the source connection pool (SQL Server), no real database. Covers: reuse within
the TTL, health check that recycles a dead connection, a connection does not return to the pool on
error, and pool disabled = create and close (default behaviour)."""

from __future__ import annotations

import pytest

from cflow_core.conexoes import pool


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        if sql == "SELECT 1" and not self.conn.alive:
            raise RuntimeError("dead connection")

    def fetchall(self):
        return []

    def close(self):
        pass


class FakeConn:
    def __init__(self):
        self.alive = True
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class _Settings:
    def __init__(self, ligado=True, ttl=60, maximo=4):
        self.pool_fontes = ligado
        self.pool_fontes_ttl_segundos = ttl
        self.pool_fontes_max = maximo


CFG = {"host": "h", "porta": 1433, "base": "b", "usuario": "u"}


@pytest.fixture
def criar(monkeypatch):
    monkeypatch.setattr(pool, "get_settings", lambda: _Settings())
    pool.fechar_sqlserver()  # clean state
    conns: list[FakeConn] = []

    def _criar():
        c = FakeConn()
        conns.append(c)
        return c

    _criar.conns = conns  # type: ignore[attr-defined]
    yield _criar
    pool.fechar_sqlserver()


def test_reusa_conexao_viva(criar):
    with pool.sqlserver_conn(CFG, criar, usar_pool=True) as c1:
        pass
    with pool.sqlserver_conn(CFG, criar, usar_pool=True) as c2:
        pass
    assert len(criar.conns) == 1  # created just one
    assert c1 is c2  # reused


def test_health_check_recicla_morta(criar):
    with pool.sqlserver_conn(CFG, criar, usar_pool=True) as c1:
        pass
    c1.alive = False  # server dropped the idle connection
    with pool.sqlserver_conn(CFG, criar, usar_pool=True) as c2:
        pass
    assert c2 is not c1
    assert c1.closed  # the dead one was closed
    assert len(criar.conns) == 2


def test_erro_nao_devolve_ao_pool(criar):
    with pytest.raises(ValueError):
        with pool.sqlserver_conn(CFG, criar, usar_pool=True) as c1:
            raise ValueError("query failed")
    assert c1.closed
    with pool.sqlserver_conn(CFG, criar, usar_pool=True) as c2:
        pass
    assert c2 is not c1  # pool was empty, so a new one was created


def test_pool_desligado_cria_e_fecha(monkeypatch):
    monkeypatch.setattr(pool, "get_settings", lambda: _Settings(ligado=False))
    criados: list[FakeConn] = []

    def _criar():
        c = FakeConn()
        criados.append(c)
        return c

    with pool.sqlserver_conn(CFG, _criar, usar_pool=False) as c:
        assert not c.closed
    assert c.closed  # closed on exit (no pool)


def test_teto_por_destino(monkeypatch):
    monkeypatch.setattr(pool, "get_settings", lambda: _Settings(maximo=1))
    pool.fechar_sqlserver()
    feitas: list[FakeConn] = []

    def _criar():
        c = FakeConn()
        feitas.append(c)
        return c

    # two concurrent (nested) connections: both exist; on return only 1 fits in the pool.
    with pool.sqlserver_conn(CFG, _criar, usar_pool=True) as a:
        with pool.sqlserver_conn(CFG, _criar, usar_pool=True) as b:
            assert a is not b
    # `b` (inner) leaves first and fits in the pool; `a` (outer) comes last, exceeds the cap of 1 and is closed.
    assert a.closed and not b.closed
    pool.fechar_sqlserver()
    assert b.closed  # fechar_sqlserver closes the idle one


def test_reaper_fecha_idle_expirada(monkeypatch):
    monkeypatch.setattr(
        pool, "get_settings", lambda: _Settings(ttl=0)
    )  # everything expires immediately
    pool.fechar_sqlserver()
    c = FakeConn()
    with pool.sqlserver_conn(CFG, lambda: c, usar_pool=True):  # returns to the pool
        pass
    assert not c.closed  # still in the pool
    fechadas = pool._expirar_sqlserver_idle()  # the reaper sweeps and closes the expired ones
    assert fechadas == 1
    assert c.closed
