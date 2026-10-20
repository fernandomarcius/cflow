"""Per-resource permission grant/revoke routes (admin). Idempotent; audited."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from cflow_core.audit import Acao, registrar
from cflow_core.authz import permissoes as perm
from cflow_core.authz.perfis import Nivel, Perfil
from cflow_core.db import transacao
from cflow_core.errors import ErroAplicacao
from cflow_core.request_context import request_id_atual

from ..deps import exigir_perfil

router = APIRouter()
SoAdmin = Depends(exigir_perfil(Perfil.ADMIN))


class PermissaoIn(BaseModel):
    sujeito_tipo: str  # usuario | grupo
    sujeito_id: uuid.UUID
    recurso_tipo: str  # pipeline | conexao | cubo | view | pagina
    recurso_id: uuid.UUID
    nivel: str  # ver | executar | editar | administrar


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _validar(dados: PermissaoIn) -> None:
    if dados.sujeito_tipo not in ("usuario", "grupo"):
        raise ErroAplicacao(codigo="sujeito_invalido", mensagem="Invalid subject.", status=400)
    if dados.nivel not in set(Nivel):
        raise ErroAplicacao(codigo="nivel_invalido", mensagem="Invalid level.", status=400)


@router.post("/permissoes", status_code=201)
async def conceder(dados: PermissaoIn, request: Request, ator: uuid.UUID = SoAdmin) -> dict:
    _validar(dados)
    async with transacao() as conn:
        criou = await perm.conceder(
            conn,
            sujeito_tipo=dados.sujeito_tipo,
            sujeito_id=dados.sujeito_id,
            recurso_tipo=dados.recurso_tipo,
            recurso_id=dados.recurso_id,
            nivel=dados.nivel,
            concedido_por=ator,
        )
        if criou:
            await registrar(
                conn,
                acao=Acao.CRIAR,
                recurso_tipo="permissao",
                recurso_id=dados.recurso_id,
                ator_id=ator,
                antes=None,
                depois=dados.model_dump(mode="json"),
                request_id=request_id_atual(),
                ip=_ip(request),
            )
    return {"criada": criou}


@router.delete("/permissoes", status_code=204)
async def revogar(dados: PermissaoIn, request: Request, ator: uuid.UUID = SoAdmin) -> Response:
    _validar(dados)
    async with transacao() as conn:
        await perm.revogar(
            conn,
            sujeito_tipo=dados.sujeito_tipo,
            sujeito_id=dados.sujeito_id,
            recurso_tipo=dados.recurso_tipo,
            recurso_id=dados.recurso_id,
            nivel=dados.nivel,
        )
        await registrar(
            conn,
            acao=Acao.EXCLUIR,
            recurso_tipo="permissao",
            recurso_id=dados.recurso_id,
            ator_id=ator,
            antes=dados.model_dump(mode="json"),
            depois=None,
            request_id=request_id_atual(),
            ip=_ip(request),
        )
    return Response(status_code=204)
