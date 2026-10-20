"""Audit routes: query (read-only), export (itself audited) and log retention (admin).

Restricted to Auditor/Admin at the edge. No route alters or deletes the trail (read-only).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from cflow_core.audit import Acao, registrar
from cflow_core.auditoria import consulta, exportacao, retencao
from cflow_core.authz.perfis import Perfil
from cflow_core.db import transacao
from cflow_core.request_context import request_id_atual
from cflow_core.timezone import para_exibicao

from ..deps import exigir_perfil

router = APIRouter()
AuditorOuAdmin = Depends(exigir_perfil(Perfil.AUDITOR, Perfil.ADMIN))
SoAdmin = Depends(exigir_perfil(Perfil.ADMIN))


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _filtros(ator_id, recurso_tipo, recurso_id, acao, de, ate) -> dict:
    return {
        "ator_id": ator_id,
        "recurso_tipo": recurso_tipo,
        "recurso_id": recurso_id,
        "acao": acao,
        "de": de,
        "ate": ate,
    }


def _serializar(linha: dict) -> dict:
    d = dict(linha)
    if d.get("ocorrido_em"):
        d["ocorrido_em"] = para_exibicao(d["ocorrido_em"]).isoformat()
    for k in ("id", "ator_id", "recurso_id"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    return d


@router.get("/auditoria")
async def consultar_trilha(
    ator: uuid.UUID = AuditorOuAdmin,
    ator_id: uuid.UUID | None = Query(default=None),
    recurso_tipo: str | None = Query(default=None),
    recurso_id: uuid.UUID | None = Query(default=None),
    acao: str | None = Query(default=None),
    de: datetime | None = Query(default=None),
    ate: datetime | None = Query(default=None),
    pagina: int = Query(default=1, ge=1),
    tamanho: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    # querying is NOT audited (read)
    async with transacao() as conn:
        linhas = await consulta.consultar(
            conn,
            ator_id=ator_id,
            recurso_tipo=recurso_tipo,
            recurso_id=recurso_id,
            acao=acao,
            de=de,
            ate=ate,
            pagina=pagina,
            tamanho=tamanho,
        )
    return [_serializar(linha) for linha in linhas]


@router.get("/auditoria/exportacao")
async def exportar_trilha(
    request: Request,
    ator: uuid.UUID = AuditorOuAdmin,
    formato: str = Query(default="csv"),
    ator_id: uuid.UUID | None = Query(default=None),
    recurso_tipo: str | None = Query(default=None),
    recurso_id: uuid.UUID | None = Query(default=None),
    acao: str | None = Query(default=None),
    de: datetime | None = Query(default=None),
    ate: datetime | None = Query(default=None),
) -> Response:
    filtros = _filtros(ator_id, recurso_tipo, recurso_id, acao, de, ate)
    async with transacao() as conn:
        linhas = await consulta.consultar(conn, **filtros, pagina=1, tamanho=500)
        # the EXPORT is audited (same transaction): actor, filters, count
        await registrar(
            conn,
            acao=Acao.EXPORTAR,
            recurso_tipo="auditoria",
            ator_id=ator,
            depois={
                "formato": formato,
                "contagem": len(linhas),
                "filtros": {k: str(v) for k, v in filtros.items() if v is not None},
            },
            request_id=request_id_atual(),
            ip=_ip(request),
        )

    if formato == "xlsx":
        conteudo = exportacao.para_xlsx(linhas)
        midia = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        nome = "auditoria.xlsx"
    else:
        conteudo = exportacao.para_csv(linhas).encode("utf-8")
        midia = "text/csv; charset=utf-8"
        nome = "auditoria.csv"
    return Response(
        content=conteudo,
        media_type=midia,
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


class RetencaoIn(BaseModel):
    dias: int


@router.patch("/admin/retencao-logs", status_code=204)
async def alterar_retencao(
    dados: RetencaoIn, request: Request, ator: uuid.UUID = SoAdmin
) -> Response:
    async with transacao() as conn:
        anterior = await retencao.definir_retencao_dias(conn, dados.dias)
        await registrar(
            conn,
            acao=Acao.RETENCAO_ALTERADA,
            recurso_tipo="configuracao",
            ator_id=ator,
            antes={"log_retencao_dias": anterior},
            depois={"log_retencao_dias": dados.dias},
            request_id=request_id_atual(),
            ip=_ip(request),
        )
    return Response(status_code=204)
