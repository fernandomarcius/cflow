"""Reading the external source must NOT happen with a metadata transaction open.

Regression of a problem measured in production: `conexoes.amostra` received the caller's `conn`
and called the connector while it was still open, so the metadata Postgres transaction stayed
alive for the whole source read (minutes, on a large SQL Server). A backend sat `idle in
transaction` for 33 minutes, its last query being the vault read.

Why this is serious and not just ugly: an open transaction holds back the `xmin` of the WHOLE
database. While it exists, autovacuum cannot reclaim dead tuples in any table. With one run a
day it goes unnoticed; with several overlapping runs a day the `xmin` stays pinned almost all the
time and bloat only grows.

The test spies on `cflow_core.db.transacao` (counts how many are open) and on the connector
(records the count at the exact moment of the read). The assertion is the invariant: **zero open
transactions while talking to the source**.
"""

from __future__ import annotations

import tempfile
import uuid
from contextlib import asynccontextmanager

import pytest

from cflow_core import db
from cflow_core.conexoes import service as cs
from cflow_core.conexoes.conectores import get_conector

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _contador(monkeypatch, estado: dict):
    """Replace `transacao` in the service module with a spy that counts nesting."""
    original = db.transacao

    @asynccontextmanager
    async def espiao():
        estado["abertas"] += 1
        try:
            async with original() as conn:
                yield conn
        finally:
            estado["abertas"] -= 1

    monkeypatch.setattr(cs, "transacao", espiao)
    yield


async def _conexao_csv(cliente, h, nome: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        f.write("account,valor\n1,100\n2,200\n")
        caminho = f.name
    r = await cliente.post(
        "/conexoes",
        json={"nome": nome, "tipo": "arquivo", "config": {"caminho": caminho}},
        headers=h,
    )
    return r.json()["id"]


async def test_amostra_le_a_fonte_sem_transacao_aberta(owner_engine, cliente, monkeypatch):
    _, h = await usuario_logado(
        owner_engine, cliente, email="tx1@example.com", perfil="especialista"
    )
    cid = await _conexao_csv(cliente, h, "tx-amostra")

    estado = {"abertas": 0, "durante_leitura": None}
    conector = get_conector("arquivo")
    original_amostra = conector.amostra

    async def amostra_espia(*args, **kwargs):
        estado["durante_leitura"] = estado["abertas"]
        return await original_amostra(*args, **kwargs)

    monkeypatch.setattr(conector, "amostra", amostra_espia)

    async with _contador(monkeypatch, estado):
        cols, linhas = await cs.amostra(uuid.UUID(cid), "", 10)

    assert cols == ["account", "valor"] and len(linhas) == 2  # really read
    assert estado["durante_leitura"] == 0, (
        "the source was read with a metadata transaction open: that pins the xmin and blocks "
        "autovacuum for the whole database"
    )
    assert estado["abertas"] == 0  # and the short transaction closed


async def test_testar_conexao_sem_transacao_aberta(owner_engine, cliente, monkeypatch):
    _, h = await usuario_logado(
        owner_engine, cliente, email="tx2@example.com", perfil="especialista"
    )
    cid = await _conexao_csv(cliente, h, "tx-testar")

    estado = {"abertas": 0, "durante_leitura": None}
    conector = get_conector("arquivo")
    original_testar = conector.testar

    async def testar_espia(*args, **kwargs):
        estado["durante_leitura"] = estado["abertas"]
        return await original_testar(*args, **kwargs)

    monkeypatch.setattr(conector, "testar", testar_espia)

    async with _contador(monkeypatch, estado):
        resultado = await cs.testar(uuid.UUID(cid))

    assert resultado["ok"] is True
    assert estado["durante_leitura"] == 0
    assert estado["abertas"] == 0


async def _pids_idle_in_transaction(engine) -> set[int]:
    from sqlalchemy import text

    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT pid FROM pg_stat_activity "
                "WHERE datname = current_database() AND state = 'idle in transaction'"
            )
        )
        return {r[0] for r in rows}


async def test_rota_de_amostra_nao_deixa_backend_idle_in_transaction(
    owner_engine, cliente, monkeypatch
):
    """End-to-end proof, in the vocabulary of the symptom seen in production.

    The module spy in the tests above only sees the transaction the service ITSELF opens; a caller
    that wrapped the read in an `async with transacao()` again would escape it. Here the check is
    on the Postgres side: during the source read, no NEW backend may be `idle in transaction`.
    That is exactly what shows in `pg_stat_activity` when the problem returns. The baseline
    discards whatever the test's own fixtures already left open.
    """
    _, h = await usuario_logado(
        owner_engine, cliente, email="tx4@example.com", perfil="especialista"
    )
    cid = await _conexao_csv(cliente, h, "tx-rota")

    base = await _pids_idle_in_transaction(owner_engine)
    visto: dict = {}

    conector = get_conector("arquivo")
    original_amostra = conector.amostra

    async def amostra_espia(*args, **kwargs):
        visto["novos"] = await _pids_idle_in_transaction(owner_engine) - base
        return await original_amostra(*args, **kwargs)

    monkeypatch.setattr(conector, "amostra", amostra_espia)

    r = await cliente.post(f"/conexoes/{cid}/amostra", json={"sql": "", "limite": 10}, headers=h)
    assert r.status_code == 200 and len(r.json()["linhas"]) == 2

    assert visto.get("novos") == set(), (
        f"backend(s) 'idle in transaction' during the source read: {visto.get('novos')}"
    )


async def test_credencial_resolvida_antes_de_falar_com_a_fonte(owner_engine, cliente, monkeypatch):
    """The secret is still resolved: the short transaction exists precisely for that.

    Without this assertion the test above would also pass with an implementation that simply
    stopped reading the vault: 'no open transaction' would be true for the wrong reason.
    """
    _, h = await usuario_logado(
        owner_engine, cliente, email="tx3@example.com", perfil="especialista"
    )
    r = await cliente.post(
        "/conexoes",
        json={
            "nome": "tx-cofre",
            "tipo": "postgres",
            "config": {"host": "localhost", "porta": 1, "banco": "x", "usuario": "u"},
            "segredo": "vault-password",
        },
        headers=h,
    )
    cid = r.json()["id"]

    acesso = await cs.obter_acesso(uuid.UUID(cid))
    assert acesso.tipo == "postgres"
    assert acesso.senha == "vault-password"  # resolved from the vault, at runtime


async def test_acesso_em_cache_e_esquecido_quando_a_credencial_muda(
    owner_engine, cliente, monkeypatch
):
    """Access is cached with a short TTL (a run with many source nodes asks for the same few
    connections), which is why INVALIDATION is what needs testing: without it, the old credential
    would stay valid for a minute in this process after being changed.

    The cache itself shows up here as "the second call does not hit the database"; what the test
    GUARANTEES is that editing the connection drops the entry immediately."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="tx-cache@example.com", perfil="especialista"
    )
    r = await cliente.post(
        "/conexoes",
        json={
            "nome": "tx-cache",
            "tipo": "postgres",
            "config": {"host": "localhost", "porta": 1, "banco": "x", "usuario": "u"},
            "segredo": "old-password",
        },
        headers=h,
    )
    cid = r.json()["id"]

    primeiro = await cs.obter_acesso(uuid.UUID(cid))
    assert primeiro.senha == "old-password"
    assert await cs.obter_acesso(uuid.UUID(cid)) is primeiro, "the second one comes from the cache"

    r = await cliente.put(
        f"/conexoes/{cid}",
        json={
            "config": {"host": "localhost", "porta": 1, "banco": "x", "usuario": "u"},
            "credencial": "new-password",  # PUT calls it `credencial`; only POST uses `segredo`
        },
        headers=h,
    )
    assert r.status_code in (200, 204), r.text

    depois = await cs.obter_acesso(uuid.UUID(cid))
    assert depois.senha == "new-password", "a changed credential must not stay cached"
    assert depois is not primeiro
