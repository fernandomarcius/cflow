"""Thin function-library service: resolves the PUBLISHED versions through the
`versao_publicada_id` pointer. The versioned CRUD (create/draft/publish/delete) reuses
`cflow_core.modelagem.versionado` with this entity's tables; nothing is duplicated here.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def obter_publicada(conn: AsyncConnection, ids: list[uuid.UUID]) -> list[dict]:
    """For each library, return the definition of the PUBLISHED version (through the
    `versao_publicada_id` pointer). Items without a published version are silently ignored; the
    order of `ids` is preserved. Returns [{"id": str, "numero": int, "definicao": dict}, ...]."""
    out: list[dict] = []
    for rid in ids:
        row = (
            await conn.execute(
                text(
                    "SELECT v.numero, v.definicao FROM biblioteca_funcoes b "
                    "JOIN biblioteca_funcoes_versoes v ON v.id = b.versao_publicada_id "
                    "WHERE b.id = :r"
                ),
                {"r": rid},
            )
        ).first()
        if row is not None:
            out.append({"id": str(rid), "numero": row.numero, "definicao": row.definicao})
    return out


async def resolver_pin(conn: AsyncConnection, ids: list[uuid.UUID]) -> list[dict]:
    """Return the published version number of each library, to pin in
    `execucoes.bibliotecas_versoes`. Ignores ids without a published version; preserves order.
    Returns [{"id": str, "numero": int}, ...]."""
    out: list[dict] = []
    for rid in ids:
        numero = (
            await conn.execute(
                text(
                    "SELECT v.numero FROM biblioteca_funcoes b "
                    "JOIN biblioteca_funcoes_versoes v ON v.id = b.versao_publicada_id "
                    "WHERE b.id = :r"
                ),
                {"r": rid},
            )
        ).scalar_one_or_none()
        if numero is not None:
            out.append({"id": str(rid), "numero": numero})
    return out
