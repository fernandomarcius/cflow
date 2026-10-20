"""Cube routes: dimensions + named measures. Versioned JSON definition; the query aggregates the
snapshot of the source pipeline's execution. Editing requires specialist/admin."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from cflow_core.authz import permissoes, sistema
from cflow_core.authz.perfis import Perfil
from cflow_core.db import transacao
from cflow_core.modelagem import consulta, resumo, versionado
from cflow_core.modelagem.definicao import validar_cubo
from cflow_core.request_context import request_id_atual

from ..deps import exigir_perfil
from .views import PublicadaOut

router = APIRouter()
Editor = Depends(exigir_perfil(Perfil.ESPECIALISTA, Perfil.ADMIN))
SoAdmin = Depends(exigir_perfil(Perfil.ADMIN))
_T = {"tabela": "cubos", "versoes": "cubo_versoes", "idcol": "cubo_id", "recurso_tipo": "cubo"}


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


class ConsultaIn(BaseModel):
    dimensoes: list[str] = []
    medidas: list[str] | None = None
    filtros: list[dict] | None = None


class SistemaIn(BaseModel):
    sistema: bool


class ResumoCuboOut(BaseModel):
    """Cube state for the LIST (see `cflow_core.modelagem.resumo`). Counts and source come from the
    LATEST version; `publicada` is the active one; `views` = visible views whose latest version reads the cube."""

    dimensoes: int
    medidas: int
    pipeline_id: str | None
    no_id: str | None
    ultimo: int
    publicada: PublicadaOut | None
    views: int


class CuboListaOut(BaseModel):
    id: str
    nome: str
    sistema: bool
    # Only with `?resumo=true`; without the parameter the response is the usual one, without the key.
    resumo: ResumoCuboOut | None = None


@router.post("/cubos")
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


@router.get("/cubos", response_model=list[CuboListaOut], response_model_exclude_unset=True)
async def listar(
    resumo_: bool = Query(
        False,
        alias="resumo",
        description="Includes a per-cube `resumo` (definition, versions and the views that depend on it) "
        "in a single query, so the Studio list no longer requests detail and versions item by item.",
    ),
    ator: uuid.UUID = Editor,
) -> list[dict]:
    async with transacao() as conn:
        adm = await sistema.eh_admin(conn, ator)
        if resumo_:
            return await resumo.listar_cubos(conn, incluir_sistema=adm)
        return await versionado.listar(conn, _T["tabela"], incluir_sistema=adm)


@router.get("/cubos/{cubo_id}")
async def obter(cubo_id: uuid.UUID, numero: int | None = None, ator: uuid.UUID = Editor) -> dict:
    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, _T["recurso_tipo"], cubo_id)
        return await versionado.obter(
            conn,
            tabela=_T["tabela"],
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_id=cubo_id,
            numero=numero,
        )


@router.get("/cubos/{cubo_id}/versoes")
async def versoes(cubo_id: uuid.UUID, ator: uuid.UUID = Editor) -> list[dict]:
    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, _T["recurso_tipo"], cubo_id)
        return await versionado.listar_versoes(
            conn, tabela=_T["tabela"], versoes=_T["versoes"], idcol=_T["idcol"], recurso_id=cubo_id
        )


@router.patch("/cubos/{cubo_id}/sistema")
async def definir_sistema(
    cubo_id: uuid.UUID, dados: SistemaIn, request: Request, ator: uuid.UUID = SoAdmin
) -> dict:
    async with transacao() as conn:
        return await versionado.definir_sistema(
            conn,
            tabela=_T["tabela"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=cubo_id,
            sistema=dados.sistema,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )


@router.delete("/cubos/{cubo_id}", status_code=204)
async def excluir(cubo_id: uuid.UUID, request: Request, ator: uuid.UUID = Editor) -> Response:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "cubo", cubo_id, "administrar")
        await versionado.excluir(
            conn,
            tabela=_T["tabela"],
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=cubo_id,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return Response(status_code=204)


@router.patch("/cubos/{cubo_id}")
async def renomear(
    cubo_id: uuid.UUID, dados: RenomearIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "cubo", cubo_id, "administrar")
        return await versionado.renomear(
            conn,
            tabela=_T["tabela"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=cubo_id,
            nome=dados.nome,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )


@router.put("/cubos/{cubo_id}/rascunho")
async def rascunho(
    cubo_id: uuid.UUID, dados: RascunhoIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "cubo", cubo_id, "editar")
        numero = await versionado.salvar_rascunho(
            conn,
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=cubo_id,
            definicao=dados.definicao,
            base_numero=dados.base_numero,
            autor_id=ator,
            validar=validar_cubo,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"numero": numero}


@router.post("/cubos/{cubo_id}/publicar")
async def publicar(
    cubo_id: uuid.UUID, dados: PublicarIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "cubo", cubo_id, "editar")
        numero = await versionado.publicar(
            conn,
            tabela=_T["tabela"],
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=cubo_id,
            base_numero=dados.base_numero,
            nota=dados.nota,
            autor_id=ator,
            validar=validar_cubo,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"numero": numero}


@router.post("/cubos/{cubo_id}/consultar")
async def consultar(cubo_id: uuid.UUID, dados: ConsultaIn, ator: uuid.UUID = Editor) -> dict:
    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, _T["recurso_tipo"], cubo_id)
        return await consulta.consultar_cubo(
            conn, cubo_id, dimensoes=dados.dimensoes, medidas=dados.medidas, filtros=dados.filtros
        )
