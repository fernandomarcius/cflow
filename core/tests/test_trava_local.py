"""LOCAL queue of `db.trava_consultiva`: whoever waits for a key lock does not hold a pool
connection parked on `pg_advisory_xact_lock`. At most one connection per key and per process waits
in the database; different keys do not wait for each other. No database: the transaction is a double
that counts the open ones."""

from __future__ import annotations

import asyncio
import contextlib

from cflow_core import db


class _Conn:
    async def execute(self, *_a, **_k):
        await asyncio.sleep(0.01)


def _contar(monkeypatch):
    estado = {"abertas": 0, "pico": 0}

    @contextlib.asynccontextmanager
    async def _transacao():
        estado["abertas"] += 1
        estado["pico"] = max(estado["pico"], estado["abertas"])
        try:
            yield _Conn()
        finally:
            estado["abertas"] -= 1

    monkeypatch.setattr(db, "transacao", _transacao)
    return estado


async def test_mesma_chave_usa_uma_conexao_por_vez(monkeypatch):
    estado = _contar(monkeypatch)

    async def _um():
        async with db.trava_consultiva("cflow:lake:gold:dev/ref/x"):
            await asyncio.sleep(0.01)

    await asyncio.gather(*(_um() for _ in range(10)))
    assert estado["pico"] == 1


async def test_chaves_diferentes_nao_se_esperam(monkeypatch):
    estado = _contar(monkeypatch)

    async def _um(i):
        async with db.trava_consultiva(f"cflow:lake:gold:dev/ref/{i}"):
            await asyncio.sleep(0.02)

    await asyncio.gather(*(_um(i) for i in range(5)))
    assert estado["pico"] == 5
