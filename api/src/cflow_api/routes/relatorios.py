"""Reports layer: declarative layouts and exports as a run artifact.

Authorization at the edge:
- **Layouts** (create, draft, publish, delete): specialist/admin - this is authoring, like pages.
- **Exports** (request, follow, download): any logged-in user - this is the operator surface,
  which only consumes. Requesting is not authoring: the layout is already published and the
  request just materializes a snapshot that already exists.

Temporal vocabulary does not leak: callers see `gerando | pronto | falha`.
Registered BEFORE `paginas` in `main.py` - that router has `GET /relatorios/{uuid}`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from cflow_core.authz.perfis import Perfil
from cflow_core.db import transacao
from cflow_core.errors import ErroAplicacao
from cflow_core.relatorios import service
from cflow_core.request_context import request_id_atual

from ..deps import exigir_perfil, usuario_atual

router = APIRouter(prefix="/relatorios")
Editor = Depends(exigir_perfil(Perfil.ESPECIALISTA, Perfil.ADMIN))
Logado = Depends(usuario_atual)

# Parameters that legacy export clients used to send and that are NOT report parameters:
# `projectId` becomes a node column (filter it in Excel) and the others CHANGE THE CALCULATION - they
# belong to the run. Any field beyond the accepted ones is rejected; these are named in the message
# because they are the ones most likely to show up.
_PARAMETROS_DE_EXECUCAO = ("projectId", "valor", "operador", "data_fim_op", "dataRegistro")


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


class CriarLayoutIn(BaseModel):
    nome: str
    descricao: str | None = None


class RascunhoIn(BaseModel):
    definicao: dict
    base_numero: int


class PublicarIn(BaseModel):
    base_numero: int
    nota: str | None = None
    nome: str | None = None


class ExportacaoIn(BaseModel):
    """Two kinds of request, one body: from ONE source (`execucao_id` + `no_id`) or GROUPED
    (`partes`, the sheet -> run map of the grouped layout). One or the other - never both, never
    neither."""

    # `extra="allow"` only to REJECT with a dedicated code: `forbid` would return the framework's
    # generic 422, and the excel-generation facade must tell apart "parameter that is not ours".
    model_config = ConfigDict(extra="allow")

    execucao_id: uuid.UUID | None = None
    no_id: str | None = None
    partes: dict[str, uuid.UUID] | None = None
    layout_id: uuid.UUID
    formato: str = "xlsx"


# --- Layouts ----------------------------------------------------------------------------------


@router.get("/layouts")
async def listar_layouts(
    pipeline_id: uuid.UUID | None = Query(default=None),
    tipo: str | None = Query(default=None),
    ator: uuid.UUID = Logado,
) -> list[dict]:
    """`tipo=agrupado` lists the multi-sheet layouts, each with `abas: [{aba, layout_id}]`;
    `tipo=simples` lists the single-source ones."""
    async with transacao() as conn:
        return await service.listar_layouts(conn, pipeline_id=pipeline_id, tipo=tipo)


@router.post("/layouts", status_code=201)
async def criar_layout(dados: CriarLayoutIn, request: Request, ator: uuid.UUID = Editor) -> dict:
    nome = dados.nome.strip()
    if not nome:
        raise ErroAplicacao(codigo="nome_invalido", mensagem="Provide a name.", status=400)
    async with transacao() as conn:
        lid = await service.criar_layout(
            conn,
            nome=nome,
            descricao=dados.descricao,
            dono_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"id": str(lid)}


@router.get("/layouts/{layout_id}")
async def obter_layout(
    layout_id: uuid.UUID, numero: int | None = Query(default=None), ator: uuid.UUID = Logado
) -> dict:
    async with transacao() as conn:
        return await service.obter_layout(conn, layout_id, numero)


@router.put("/layouts/{layout_id}/rascunho")
async def salvar_rascunho(
    layout_id: uuid.UUID, dados: RascunhoIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        numero = await service.salvar_rascunho_layout(
            conn,
            layout_id=layout_id,
            definicao=dados.definicao,
            base_numero=dados.base_numero,
            autor_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"numero": numero}


@router.post("/layouts/{layout_id}/publicar")
async def publicar_layout(
    layout_id: uuid.UUID, dados: PublicarIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        return await service.publicar_layout(
            conn,
            layout_id=layout_id,
            base_numero=dados.base_numero,
            nota=dados.nota,
            nome=dados.nome,
            autor_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )


@router.delete("/layouts/{layout_id}", status_code=204)
async def excluir_layout(
    layout_id: uuid.UUID, request: Request, ator: uuid.UUID = Editor
) -> Response:
    async with transacao() as conn:
        await service.excluir_layout(
            conn,
            layout_id=layout_id,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return Response(status_code=204)


# --- Exports ----------------------------------------------------------------------------------


@router.post("/exportacoes")
async def solicitar_exportacao(
    dados: ExportacaoIn, request: Request, response: Response, ator: uuid.UUID = Logado
) -> dict:
    """Request the artifact for the tuple (run, node, layout, format). **200** `pronto` -> download
    at `.../{id}/arquivo`; **202** `gerando` -> follow with `GET .../{id}`; **409**
    `execucao_nao_concluida`. Idempotent: the same request returns the same record.

    **Grouped** (layout `tipo: agrupado`): `{"layout_id", "partes": {"SALES": <execucao_id>,
    ...}}` - one run per sheet, without `execucao_id`/`no_id` (each sheet's node is its own
    layout's). Same status codes, same idempotency; a part's error names the sheet."""
    extras = sorted(set(dados.model_extra or {}))
    if extras:
        nomeados = [p for p in _PARAMETROS_DE_EXECUCAO if p in extras]
        raise ErroAplicacao(
            codigo="parametro_nao_e_do_relatorio",
            mensagem="Parameter that does not belong to the report: "
            + ", ".join(extras)
            + ". The report is the WHOLE snapshot of a completed run's node"
            + (
                " - projectId is a node column (filter in Excel) and "
                + ", ".join(p for p in nomeados if p != "projectId")
                + " change the calculation: they belong to the run, not to the report."
                if nomeados
                else "."
            ),
            status=400,
        )
    if dados.partes is not None:
        if dados.execucao_id is not None or dados.no_id is not None:
            raise ErroAplicacao(
                codigo="pedido_invalido",
                mensagem="Provide 'partes' (grouped export) OR 'execucao_id' and 'no_id' "
                "(single-source export), not both.",
                status=400,
            )
        res = await service.solicitar_agrupado(
            layout_id=dados.layout_id,
            partes=dados.partes,
            ator_id=ator,
            formato=dados.formato,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    else:
        if dados.execucao_id is None or not dados.no_id:
            raise ErroAplicacao(
                codigo="pedido_invalido",
                mensagem="Provide 'execucao_id' and 'no_id' (single-source export) or 'partes' "
                "(grouped export: sheet -> run).",
                status=400,
            )
        res = await service.solicitar(
            execucao_id=dados.execucao_id,
            no_id=dados.no_id,
            layout_id=dados.layout_id,
            ator_id=ator,
            formato=dados.formato,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    exportacao = res["exportacao"]
    response.status_code = 200 if exportacao["estado"] == "pronto" else 202
    return exportacao


@router.get("/exportacoes")
async def listar_exportacoes(
    execucao_id: uuid.UUID | None = Query(default=None),
    limite: int = Query(default=100, ge=1, le=500),
    ator: uuid.UUID = Logado,
) -> list[dict]:
    async with transacao() as conn:
        return await service.listar(conn, execucao_id=execucao_id, limite=limite)


@router.get("/exportacoes/{exportacao_id}")
async def obter_exportacao(exportacao_id: uuid.UUID, ator: uuid.UUID = Logado) -> dict:
    async with transacao() as conn:
        return await service.obter(conn, exportacao_id)


@router.get("/exportacoes/{exportacao_id}/arquivo")
async def baixar_arquivo(exportacao_id: uuid.UUID, ator: uuid.UUID = Logado) -> FileResponse:
    """Stream the artifact with the session - never a signed URL (air-gapped). 409 if not ready."""
    arq = await service.arquivo(exportacao_id)
    return FileResponse(
        arq["caminho"],
        media_type=arq["tipo_mime"],
        filename=arq["nome_arquivo"],
    )
