"""Function library routes (Studio): reusable DuckDB MACROs. Versioned JSON definition
(draft != published, audited). Editing requires specialist/admin. `/avaliar` tests a macro call
against sample values in an ephemeral DuckDB, as an editing aid in the Studio."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from cflow_core.authz import permissoes, sistema
from cflow_core.authz.perfis import Perfil
from cflow_core.biblioteca_funcoes import service as biblioteca
from cflow_core.biblioteca_funcoes.compilar import montar_ddl
from cflow_core.biblioteca_funcoes.definicao import validar
from cflow_core.db import transacao
from cflow_core.errors import ErroAplicacao
from cflow_core.modelagem import versionado
from cflow_core.request_context import request_id_atual

from ..deps import exigir_perfil

router = APIRouter()
Editor = Depends(exigir_perfil(Perfil.ESPECIALISTA, Perfil.ADMIN))
SoAdmin = Depends(exigir_perfil(Perfil.ADMIN))
_T = {
    "tabela": "biblioteca_funcoes",
    "versoes": "biblioteca_funcoes_versoes",
    "idcol": "biblioteca_id",
    "recurso_tipo": "biblioteca_funcao",
}


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


class AvaliarIn(BaseModel):
    chamada: str
    definicao: dict | None = None
    argumentos: dict | None = None


class SistemaIn(BaseModel):
    sistema: bool


@router.post("/funcoes")
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


@router.get("/funcoes")
async def listar(ator: uuid.UUID = Editor) -> list[dict]:
    async with transacao() as conn:
        adm = await sistema.eh_admin(conn, ator)
        return await versionado.listar(conn, _T["tabela"], incluir_sistema=adm)


@router.get("/funcoes/{biblioteca_id}")
async def obter(
    biblioteca_id: uuid.UUID, numero: int | None = None, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, _T["recurso_tipo"], biblioteca_id)
        return await versionado.obter(
            conn,
            tabela=_T["tabela"],
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_id=biblioteca_id,
            numero=numero,
        )


@router.get("/funcoes/{biblioteca_id}/versoes")
async def versoes(biblioteca_id: uuid.UUID, ator: uuid.UUID = Editor) -> list[dict]:
    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, _T["recurso_tipo"], biblioteca_id)
        return await versionado.listar_versoes(
            conn,
            tabela=_T["tabela"],
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_id=biblioteca_id,
        )


@router.patch("/funcoes/{biblioteca_id}/sistema")
async def definir_sistema(
    biblioteca_id: uuid.UUID, dados: SistemaIn, request: Request, ator: uuid.UUID = SoAdmin
) -> dict:
    async with transacao() as conn:
        return await versionado.definir_sistema(
            conn,
            tabela=_T["tabela"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=biblioteca_id,
            sistema=dados.sistema,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )


@router.delete("/funcoes/{biblioteca_id}", status_code=204)
async def excluir(biblioteca_id: uuid.UUID, request: Request, ator: uuid.UUID = Editor) -> Response:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "biblioteca_funcao", biblioteca_id, "administrar")
        await versionado.excluir(
            conn,
            tabela=_T["tabela"],
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=biblioteca_id,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return Response(status_code=204)


@router.patch("/funcoes/{biblioteca_id}")
async def renomear(
    biblioteca_id: uuid.UUID, dados: RenomearIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "biblioteca_funcao", biblioteca_id, "administrar")
        return await versionado.renomear(
            conn,
            tabela=_T["tabela"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=biblioteca_id,
            nome=dados.nome,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )


@router.put("/funcoes/{biblioteca_id}/rascunho")
async def rascunho(
    biblioteca_id: uuid.UUID, dados: RascunhoIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "biblioteca_funcao", biblioteca_id, "editar")
        numero = await versionado.salvar_rascunho(
            conn,
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=biblioteca_id,
            definicao=dados.definicao,
            base_numero=dados.base_numero,
            autor_id=ator,
            validar=validar,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"numero": numero}


@router.post("/funcoes/{biblioteca_id}/publicar")
async def publicar(
    biblioteca_id: uuid.UUID, dados: PublicarIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "biblioteca_funcao", biblioteca_id, "editar")
        numero = await versionado.publicar(
            conn,
            tabela=_T["tabela"],
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=biblioteca_id,
            base_numero=dados.base_numero,
            nota=dados.nota,
            autor_id=ator,
            validar=validar,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"numero": numero}


@router.post("/funcoes/{biblioteca_id}/avaliar")
async def avaliar(biblioteca_id: uuid.UUID, dados: AvaliarIn, ator: uuid.UUID = Editor) -> dict:
    """Test a macro call against sample values. Loads the library macros into an ephemeral DuckDB
    and runs `SELECT <call>`. A `definicao` in the body overrides the saved version (editing
    preview); otherwise uses the published version and, failing that, the latest draft. A DuckDB
    error becomes a generic envelope (no stack/SQL)."""
    import duckdb

    definicao = dados.definicao
    if definicao is None:
        async with transacao() as conn:
            await sistema.exigir_acesso(conn, ator, _T["recurso_tipo"], biblioteca_id)
            pub = await biblioteca.obter_publicada(conn, [biblioteca_id])
            if pub:
                definicao = pub[0]["definicao"]
            else:
                det = await versionado.obter(
                    conn,
                    tabela=_T["tabela"],
                    versoes=_T["versoes"],
                    idcol=_T["idcol"],
                    recurso_id=biblioteca_id,
                )
                definicao = det["definicao"]
    if not definicao:
        raise ErroAplicacao(
            codigo="sem_definicao", mensagem="There is no definition to evaluate.", status=400
        )

    ddl = montar_ddl(
        [definicao]
    )  # 409 on macro collision; 422 does not apply (already validated on save)

    duck = duckdb.connect()
    try:
        if ddl:
            duck.execute(ddl)
        cur = duck.execute(f"SELECT ({dados.chamada}) AS resultado")
        valor = cur.fetchone()[0]
    except Exception as exc:  # noqa: BLE001 - DuckDB has no common base class; sanitize and do not leak stack/SQL
        # keep only the first line of the DuckDB message (no stack); truncate so detail does not leak.
        saneado = (
            str(exc).strip().splitlines()[0][:200] if str(exc).strip() else "invalid expression"
        )
        raise ErroAplicacao(
            codigo="avaliacao_falha",
            mensagem=f"Failed to evaluate the function: {saneado}",
            status=400,
        ) from exc
    finally:
        duck.close()

    return {"resultado": None if valor is None else str(valor)}
