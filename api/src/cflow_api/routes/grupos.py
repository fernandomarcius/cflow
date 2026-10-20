"""Group routes (admin): list, create, delete (cascade), add/remove member. All audited."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cflow_core.audit import Acao, registrar
from cflow_core.authz.models import grupos, permissoes, usuarios, usuarios_grupos
from cflow_core.authz.perfis import Perfil
from cflow_core.db import transacao
from cflow_core.errors import ErroAplicacao
from cflow_core.request_context import request_id_atual

from ..deps import exigir_perfil

router = APIRouter()
SoAdmin = Depends(exigir_perfil(Perfil.ADMIN))


class CriarGrupoIn(BaseModel):
    nome: str
    descricao: str | None = None


class MembroIn(BaseModel):
    usuario_id: uuid.UUID


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/grupos")
async def listar_grupos(ator: uuid.UUID = SoAdmin) -> list[dict]:
    async with transacao() as conn:
        gs = (await conn.execute(select(grupos).order_by(grupos.c.nome))).mappings().all()
        membros = (
            await conn.execute(
                select(usuarios_grupos.c.grupo_id, usuarios.c.id, usuarios.c.nome, usuarios.c.email)
                .join(usuarios, usuarios.c.id == usuarios_grupos.c.usuario_id)
                .order_by(usuarios.c.nome)
            )
        ).all()
    por_grupo: dict[uuid.UUID, list[dict]] = {}
    for gid, uid, nome, email in membros:
        por_grupo.setdefault(gid, []).append({"id": str(uid), "nome": nome, "email": email})
    return [
        {
            "id": str(g["id"]),
            "nome": g["nome"],
            "descricao": g["descricao"],
            "membros": por_grupo.get(g["id"], []),
        }
        for g in gs
    ]


@router.post("/grupos", status_code=201)
async def criar_grupo(dados: CriarGrupoIn, request: Request, ator: uuid.UUID = SoAdmin) -> dict:
    gid = uuid.uuid4()
    async with transacao() as conn:
        existe = (await conn.execute(grupos.select().where(grupos.c.nome == dados.nome))).first()
        if existe is not None:
            raise ErroAplicacao(
                codigo="grupo_em_uso", mensagem="Group name already in use.", status=409
            )
        await conn.execute(
            grupos.insert(), {"id": gid, "nome": dados.nome, "descricao": dados.descricao}
        )
        await registrar(
            conn,
            acao=Acao.CRIAR,
            recurso_tipo="grupo",
            recurso_id=gid,
            ator_id=ator,
            antes=None,
            depois={"nome": dados.nome},
            request_id=request_id_atual(),
            ip=_ip(request),
        )
    return {"id": str(gid)}


@router.delete("/grupos/{grupo_id}", status_code=204)
async def excluir_grupo(
    grupo_id: uuid.UUID, request: Request, ator: uuid.UUID = SoAdmin
) -> Response:
    async with transacao() as conn:
        # cascade in the same transaction: the group's permissions + memberships + the group
        await conn.execute(
            delete(permissoes).where(
                permissoes.c.sujeito_tipo == "grupo", permissoes.c.sujeito_id == grupo_id
            )
        )
        await conn.execute(delete(usuarios_grupos).where(usuarios_grupos.c.grupo_id == grupo_id))
        await conn.execute(delete(grupos).where(grupos.c.id == grupo_id))
        await registrar(
            conn,
            acao=Acao.EXCLUIR,
            recurso_tipo="grupo",
            recurso_id=grupo_id,
            ator_id=ator,
            antes={"grupo_id": str(grupo_id)},
            depois=None,
            request_id=request_id_atual(),
            ip=_ip(request),
        )
    return Response(status_code=204)


@router.post("/grupos/{grupo_id}/membros", status_code=204)
async def add_membro(
    grupo_id: uuid.UUID, dados: MembroIn, request: Request, ator: uuid.UUID = SoAdmin
) -> Response:
    async with transacao() as conn:
        await conn.execute(
            pg_insert(usuarios_grupos)
            .values(usuario_id=dados.usuario_id, grupo_id=grupo_id)
            .on_conflict_do_nothing()
        )
        await registrar(
            conn,
            acao=Acao.CRIAR,
            recurso_tipo="usuario_grupo",
            recurso_id=grupo_id,
            ator_id=ator,
            antes=None,
            depois={"usuario_id": str(dados.usuario_id), "grupo_id": str(grupo_id)},
            request_id=request_id_atual(),
            ip=_ip(request),
        )
    return Response(status_code=204)


@router.delete("/grupos/{grupo_id}/membros/{usuario_id}", status_code=204)
async def remover_membro(
    grupo_id: uuid.UUID, usuario_id: uuid.UUID, request: Request, ator: uuid.UUID = SoAdmin
) -> Response:
    async with transacao() as conn:
        await conn.execute(
            delete(usuarios_grupos).where(
                usuarios_grupos.c.grupo_id == grupo_id, usuarios_grupos.c.usuario_id == usuario_id
            )
        )
        await registrar(
            conn,
            acao=Acao.EXCLUIR,
            recurso_tipo="usuario_grupo",
            recurso_id=grupo_id,
            ator_id=ator,
            antes={"usuario_id": str(usuario_id), "grupo_id": str(grupo_id)},
            depois=None,
            request_id=request_id_atual(),
            ip=_ip(request),
        )
    return Response(status_code=204)
