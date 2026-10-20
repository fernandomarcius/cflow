"""Filtered, paginated query over the audit trail (read-only). Filters combine; ordered by time."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from ..audit.models import auditoria


async def consultar(
    conn: AsyncConnection,
    *,
    ator_id: uuid.UUID | None = None,
    recurso_tipo: str | None = None,
    recurso_id: uuid.UUID | None = None,
    acao: str | None = None,
    de: datetime | None = None,
    ate: datetime | None = None,
    pagina: int = 1,
    tamanho: int = 50,
) -> list[dict]:
    """Return trail records matching the filters (intersection), ordered by time.

    `de`/`ate` are timezone-aware datetimes (the edge converts from the display timezone); the
    `timestamptz` comparison is done by Postgres with no loss at the boundary. Stable pagination (ocorrido_em, id).
    """
    cond = []
    if ator_id is not None:
        cond.append(auditoria.c.ator_id == ator_id)
    if recurso_tipo is not None:
        cond.append(auditoria.c.recurso_tipo == recurso_tipo)
    if recurso_id is not None:
        cond.append(auditoria.c.recurso_id == recurso_id)
    if acao is not None:
        cond.append(auditoria.c.acao == acao)
    if de is not None:
        cond.append(auditoria.c.ocorrido_em >= de)
    if ate is not None:
        cond.append(auditoria.c.ocorrido_em <= ate)

    tamanho = max(1, min(tamanho, 500))
    pagina = max(1, pagina)
    stmt = (
        select(auditoria)
        .where(*cond)
        .order_by(auditoria.c.ocorrido_em, auditoria.c.id)
        .limit(tamanho)
        .offset((pagina - 1) * tamanho)
    )
    rows = (await conn.execute(stmt)).mappings().all()
    return [dict(r) for r in rows]
