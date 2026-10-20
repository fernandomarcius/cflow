"""Single flight: per key, one job at a time IN THIS process.

Whoever arrives with the same key while the first is working waits for it and, on entering, finds
the result it left: the completed snapshot in the database (chain extraction) or the file in the
local cache (lake download). Without this, two simultaneous requests for the same thing did the
whole work twice: the cache only sees what has already FINISHED.

It is not a cross-process lock, on purpose. With several worker replicas, different replicas can
still repeat the work, which is the previous behaviour: correct and just more expensive. A
Postgres lock would cover the replicas but hold a pool connection during a multi-minute
extraction, and `idle_in_transaction_session_timeout` kills the session.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager


class VooUnico:
    def __init__(self) -> None:
        # key -> [lock, interested]. The entry disappears when nobody uses it anymore: an asyncio
        # lock binds to the event loop, and reusing it in another loop (tests) would break.
        self._travas: dict[Hashable, list] = {}

    async def entrar(self, chave: Hashable) -> Hashable:
        par = self._travas.get(chave)
        if par is None:
            par = self._travas[chave] = [asyncio.Lock(), 0]
        par[1] += 1
        try:
            await par[0].acquire()
        except BaseException:
            # Cancelled while queued: without this the entry kept counting someone who left.
            self._largar(chave, par)
            raise
        return chave

    def sair(self, chave: Hashable) -> None:
        par = self._travas[chave]
        par[0].release()
        self._largar(chave, par)

    def _largar(self, chave: Hashable, par: list) -> None:
        par[1] -= 1
        if par[1] == 0 and self._travas.get(chave) is par:
            del self._travas[chave]

    @asynccontextmanager
    async def voo(self, chave: Hashable) -> AsyncIterator[None]:
        await self.entrar(chave)
        try:
            yield
        finally:
            self.sair(chave)

    def __len__(self) -> int:
        return len(self._travas)
