"""Sample write harness, for TESTS only (no route is published for it).

Simulates a business write action that records to the audit trail in the SAME transaction.
Since the foundation has no real resources of its own here, a test resource is used.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncEngine

from cflow_core.audit import Acao, AtorTipo, registrar


async def escrever_recurso_exemplo(
    engine: AsyncEngine,
    *,
    request_id: str = "teste-req",
    falhar: bool = False,
) -> uuid.UUID:
    """Run a sample write + audit in a single transaction.

    If `falhar=True`, raise AFTER recording the audit entry, forcing a rollback. Used to prove
    atomicity: nothing may be left in the trail.
    """
    recurso_id = uuid.uuid4()
    async with engine.begin() as conn:
        await registrar(
            conn,
            acao=Acao.CRIAR,
            recurso_tipo="recurso_teste",
            recurso_id=recurso_id,
            ator_tipo=AtorTipo.SISTEMA,
            antes=None,
            depois={"nome": "exemplo"},
            request_id=request_id,
        )
        if falhar:
            raise RuntimeError("deliberate failure after recording (must roll back)")
    return recurso_id
