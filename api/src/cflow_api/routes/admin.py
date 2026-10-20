"""Administrative routes: health/observability panel and promotion bundles. Admin profile only."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from cflow_core.admin import service as admin_service
from cflow_core.authz.perfis import Perfil
from cflow_core.db import transacao
from cflow_core.request_context import request_id_atual

from ..deps import exigir_perfil

router = APIRouter()
SoAdmin = Depends(exigir_perfil(Perfil.ADMIN))


@router.get("/admin/metricas")
async def metricas(ator: uuid.UUID = SoAdmin) -> dict:
    async with transacao() as conn:
        return await admin_service.metricas(conn)


@router.get("/admin/conexoes/saude")
async def saude_conexoes(ator: uuid.UUID = SoAdmin) -> list[dict]:
    # No `transacao()` here: the service reads the list in a short transaction and tests the
    # connections outside it (each test is a network call to a third-party system).
    return await admin_service.saude_conexoes()


@router.get("/admin/exportar")
async def exportar(
    ator: uuid.UUID = SoAdmin, pipeline: list[uuid.UUID] | None = Query(default=None)
) -> dict:
    """Promotion bundle (v2): pipelines at their PUBLISHED version, in dependency order, with
    layer/group/system, and the connections WITHOUT secrets. `?pipeline=<id>` (repeatable) exports
    only their closure (what they read through a `pipeline` source, transitively); without it,
    everything."""
    async with transacao() as conn:
        return await admin_service.exportar(conn, pipeline)


class ImportarIn(BaseModel):
    bundle: dict
    atualizar: bool = False
    simular: bool = False


@router.post("/admin/importar")
async def importar(dados: ImportarIn, request: Request, ator: uuid.UUID = SoAdmin) -> dict:
    """Import the bundle: match by name, create in dependency order re-pointing connections and
    pipelines, and publish a new version of an existing pipeline only with `atualizar`. `simular`
    returns the plan without writing. All in one transaction; each write is audited by the service
    that owns it."""
    async with transacao() as conn:
        return await admin_service.importar(
            conn,
            dados.bundle,
            ator_id=ator,
            ip=request.client.host if request.client else None,
            request_id=request_id_atual(),
            atualizar=dados.atualizar,
            simular=dados.simular,
        )
