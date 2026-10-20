"""View routes: pivot layout over a cube + released filters. Editing requires specialist/admin;
QUERYING is open to any logged-in user (the Portal/operator consumes it)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from cflow_core.authz import permissoes, sistema
from cflow_core.authz.perfis import Perfil
from cflow_core.db import transacao
from cflow_core.modelagem import consulta, resumo, versionado
from cflow_core.modelagem.definicao import validar_view
from cflow_core.request_context import request_id_atual

from ..deps import exigir_perfil, usuario_atual

router = APIRouter()
Editor = Depends(exigir_perfil(Perfil.ESPECIALISTA, Perfil.ADMIN))
SoAdmin = Depends(exigir_perfil(Perfil.ADMIN))
_T = {"tabela": "views", "versoes": "view_versoes", "idcol": "view_id", "recurso_tipo": "view"}


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


class CriarIn(BaseModel):
    nome: str
    descricao: str | None = None


class RenomearIn(BaseModel):
    nome: str


class RascunhoIn(BaseModel):
    definicao: dict
    base_numero: int


class PublicarIn(BaseModel):
    base_numero: int
    nota: str | None = None
    nome: str | None = None


class ConsultaViewIn(BaseModel):
    filtros: list[dict] | None = None  # only on released dimensions


class SistemaIn(BaseModel):
    sistema: bool


class PublicadaOut(BaseModel):
    """Active published version of a versioned resource (also used by the cube list)."""

    numero: int
    criado_em: str | None


class ResumoViewOut(BaseModel):
    """View state for the LIST (see `cflow_core.modelagem.resumo`). Counts and `cubo_id` come from the
    LATEST version; `publicada` is the active one; a draft is ahead when `ultimo > publicada.numero`."""

    cubo_id: str | None
    linhas: int
    colunas: int
    valores: int
    filtros_liberados: int
    ultimo: int
    publicada: PublicadaOut | None
    paginas: int
    paginas_nao_verificadas: int


class ViewListaOut(BaseModel):
    id: str
    nome: str
    sistema: bool
    # Only with `?resumo=true`; without the parameter the response is the usual one, without the key.
    resumo: ResumoViewOut | None = None


@router.post("/views")
async def criar(dados: CriarIn, request: Request, ator: uuid.UUID = Editor) -> dict:
    async with transacao() as conn:
        rid = await versionado.criar(
            conn,
            tabela=_T["tabela"],
            recurso_tipo=_T["recurso_tipo"],
            nome=dados.nome,
            descricao=dados.descricao,
            dono_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"id": str(rid)}


@router.get("/views", response_model=list[ViewListaOut], response_model_exclude_unset=True)
async def listar(
    resumo_: bool = Query(
        False,
        alias="resumo",
        description="Includes a per-view `resumo` (definition, versions and usage in pages) in a "
        "single query, so the Studio list no longer requests detail and versions item by item.",
    ),
    ator: uuid.UUID = Depends(usuario_atual),
) -> list[dict]:
    async with transacao() as conn:
        adm = await sistema.eh_admin(conn, ator)
        if resumo_:
            return await resumo.listar_views(conn, incluir_sistema=adm)
        return await versionado.listar(conn, _T["tabela"], incluir_sistema=adm)


@router.get("/views/{view_id}")
async def obter(
    view_id: uuid.UUID, numero: int | None = None, ator: uuid.UUID = Depends(usuario_atual)
) -> dict:
    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, _T["recurso_tipo"], view_id)
        return await versionado.obter(
            conn,
            tabela=_T["tabela"],
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_id=view_id,
            numero=numero,
        )


@router.get("/views/{view_id}/versoes")
async def versoes(view_id: uuid.UUID, ator: uuid.UUID = Depends(usuario_atual)) -> list[dict]:
    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, _T["recurso_tipo"], view_id)
        return await versionado.listar_versoes(
            conn, tabela=_T["tabela"], versoes=_T["versoes"], idcol=_T["idcol"], recurso_id=view_id
        )


@router.patch("/views/{view_id}/sistema")
async def definir_sistema(
    view_id: uuid.UUID, dados: SistemaIn, request: Request, ator: uuid.UUID = SoAdmin
) -> dict:
    async with transacao() as conn:
        return await versionado.definir_sistema(
            conn,
            tabela=_T["tabela"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=view_id,
            sistema=dados.sistema,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )


@router.delete("/views/{view_id}", status_code=204)
async def excluir(view_id: uuid.UUID, request: Request, ator: uuid.UUID = Editor) -> Response:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "view", view_id, "administrar")
        await versionado.excluir(
            conn,
            tabela=_T["tabela"],
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=view_id,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return Response(status_code=204)


@router.patch("/views/{view_id}")
async def renomear(
    view_id: uuid.UUID, dados: RenomearIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "view", view_id, "administrar")
        return await versionado.renomear(
            conn,
            tabela=_T["tabela"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=view_id,
            nome=dados.nome,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )


@router.put("/views/{view_id}/rascunho")
async def rascunho(
    view_id: uuid.UUID, dados: RascunhoIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "view", view_id, "editar")
        numero = await versionado.salvar_rascunho(
            conn,
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=view_id,
            definicao=dados.definicao,
            base_numero=dados.base_numero,
            autor_id=ator,
            validar=validar_view,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"numero": numero}


@router.post("/views/{view_id}/publicar")
async def publicar(
    view_id: uuid.UUID, dados: PublicarIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "view", view_id, "editar")
        numero = await versionado.publicar(
            conn,
            tabela=_T["tabela"],
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=view_id,
            base_numero=dados.base_numero,
            nota=dados.nota,
            autor_id=ator,
            validar=validar_view,
            nome=dados.nome,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"numero": numero}


@router.post("/views/{view_id}/consultar")
async def consultar(
    view_id: uuid.UUID, dados: ConsultaViewIn, ator: uuid.UUID = Depends(usuario_atual)
) -> dict:
    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, _T["recurso_tipo"], view_id)
        return await consulta.consultar_view(conn, view_id, filtros_extra=dados.filtros)
