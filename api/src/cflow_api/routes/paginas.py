"""Page routes, catalog + query/report.

Editing a page requires specialist/admin. The catalog, query and report generation are open to any
logged-in user: it is the operator surface (Portal), read-only by construction.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import text

from cflow_core.authz import permissoes, sistema
from cflow_core.authz.perfis import Perfil
from cflow_core.db import transacao
from cflow_core.errors import ErroAplicacao
from cflow_core.modelagem import consulta, relatorio, versionado
from cflow_core.modelagem.definicao import validar_pagina
from cflow_core.pipelines import service as pipelines_service
from cflow_core.request_context import request_id_atual

from ..deps import exigir_perfil, usuario_atual

router = APIRouter()
Editor = Depends(exigir_perfil(Perfil.ESPECIALISTA, Perfil.ADMIN))
SoAdmin = Depends(exigir_perfil(Perfil.ADMIN))
Logado = Depends(usuario_atual)
_T = {
    "tabela": "paginas",
    "versoes": "pagina_versoes",
    "idcol": "pagina_id",
    "recurso_tipo": "pagina",
}

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _resposta_xlsx(conteudo: bytes, nome: str) -> Response:
    return Response(
        content=conteudo,
        media_type=_XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


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


class ConsultaIn(BaseModel):
    filtros: list[dict] | None = None
    # Execution to query (the one the operator triggered). Without it: the latest completed execution.
    execucao_id: uuid.UUID | None = None


class SistemaIn(BaseModel):
    sistema: bool


@router.post("/paginas")
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


@router.delete("/paginas/{pagina_id}", status_code=204)
async def excluir(pagina_id: uuid.UUID, request: Request, ator: uuid.UUID = Editor) -> Response:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "pagina", pagina_id, "administrar")
        await versionado.excluir(
            conn,
            tabela=_T["tabela"],
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=pagina_id,
            ator_id=ator,
            # a stamped report is immutable and part of the trace chain: it must not vanish by cascade.
            guardas=[
                (
                    "SELECT count(*) FROM relatorios WHERE pagina_id = :r",
                    "pagina_com_relatorio",
                    "The page has stamped (immutable) reports and cannot be deleted.",
                )
            ],
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return Response(status_code=204)


@router.get("/paginas/{pagina_id}")
async def obter(pagina_id: uuid.UUID, numero: int | None = None, ator: uuid.UUID = Logado) -> dict:
    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, _T["recurso_tipo"], pagina_id)
        return await versionado.obter(
            conn,
            tabela=_T["tabela"],
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_id=pagina_id,
            numero=numero,
        )


@router.get("/paginas/{pagina_id}/parametros")
async def parametros_do_operador(pagina_id: uuid.UUID, ator: uuid.UUID = Logado) -> list[dict]:
    """Parameters the operator fills in on the Portal, in the order configured on the page.

    The page's PUBLISHED version declares in `execucao.operador` which pipeline parameters are
    exposed; here we return only the DECLARATION of each one (type, required, options), never the
    rest of the pipeline definition (SQL, nodes, connections).

    Authorization is the same as `/pipelines/{id}/executar` with `pagina_id`
    (`_autorizar_execucao`): any authenticated user, because a page published in the catalog is
    consumable by everyone (read-only Portal). The operator has no access to
    `GET /pipelines/{id}`, so without this endpoint they could not build the report form they are
    already entitled to generate.
    """
    async with transacao() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT pv.definicao FROM paginas p JOIN pagina_versoes pv ON pv.id = p.versao_publicada_id "
                    "WHERE p.id = :id"
                ),
                {"id": pagina_id},
            )
        ).first()
        if row is None:
            raise ErroAplicacao(
                codigo="nao_encontrado",
                mensagem="Page not found or not published.",
                status=404,
            )
        definicao = row.definicao if isinstance(row.definicao, dict) else {}
        cfg = definicao.get("execucao") or {}
        pipeline_id = cfg.get("pipeline_id")
        if not pipeline_id:
            return []  # page without execution (only queries the snapshot): no parameter to fill in
        det = await pipelines_service.obter(conn, uuid.UUID(str(pipeline_id)), None)
        declarados = {
            p["nome"]: p
            for p in (det["definicao"].get("parametros") or [])
            if isinstance(p, dict) and p.get("nome")
        }
        saida: list[dict] = []
        for item in cfg.get("operador") or []:
            decl = declarados.get(item.get("param"))
            if decl is None:
                continue  # parameter removed from the pipeline: simply does not show up in the form
            saida.append(
                {
                    "nome": decl["nome"],
                    "rotulo": (item.get("rotulo") or "").strip() or decl["nome"],
                    # `valor_preview` belongs to the Studio preview, not to the operator.
                    "decl": {k: v for k, v in decl.items() if k != "valor_preview"},
                }
            )
        return saida


@router.get("/paginas/{pagina_id}/versoes")
async def versoes(pagina_id: uuid.UUID, ator: uuid.UUID = Logado) -> list[dict]:
    async with transacao() as conn:
        await sistema.exigir_acesso(conn, ator, _T["recurso_tipo"], pagina_id)
        return await versionado.listar_versoes(
            conn,
            tabela=_T["tabela"],
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_id=pagina_id,
        )


@router.patch("/paginas/{pagina_id}/sistema")
async def definir_sistema(
    pagina_id: uuid.UUID, dados: SistemaIn, request: Request, ator: uuid.UUID = SoAdmin
) -> dict:
    async with transacao() as conn:
        return await versionado.definir_sistema(
            conn,
            tabela=_T["tabela"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=pagina_id,
            sistema=dados.sistema,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )


@router.patch("/paginas/{pagina_id}")
async def renomear(
    pagina_id: uuid.UUID, dados: RenomearIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "pagina", pagina_id, "administrar")
        return await versionado.renomear(
            conn,
            tabela=_T["tabela"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=pagina_id,
            nome=dados.nome,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )


@router.put("/paginas/{pagina_id}/rascunho")
async def rascunho(
    pagina_id: uuid.UUID, dados: RascunhoIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "pagina", pagina_id, "editar")
        numero = await versionado.salvar_rascunho(
            conn,
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=pagina_id,
            definicao=dados.definicao,
            base_numero=dados.base_numero,
            autor_id=ator,
            validar=validar_pagina,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"numero": numero}


@router.post("/paginas/{pagina_id}/publicar")
async def publicar(
    pagina_id: uuid.UUID, dados: PublicarIn, request: Request, ator: uuid.UUID = Editor
) -> dict:
    async with transacao() as conn:
        await permissoes.exigir(conn, ator, "pagina", pagina_id, "editar")
        numero = await versionado.publicar(
            conn,
            tabela=_T["tabela"],
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=pagina_id,
            base_numero=dados.base_numero,
            nota=dados.nota,
            autor_id=ator,
            validar=validar_pagina,
            nome=dados.nome,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"numero": numero}


@router.get("/catalogo")
async def catalogo(ator: uuid.UUID = Logado) -> list[dict]:
    """Portal catalog: published pages (read-only for the operator).

    Includes the `execucao` block of the PUBLISHED version (or null), so the Portal can render the
    parameter filters and know which pipeline to run. Lenient validation: a dangling pipeline does
    not bring the catalog down; the operator simply cannot generate an execution."""
    async with transacao() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT p.id, p.nome, p.sistema, pv.definicao "
                        "FROM paginas p JOIN pagina_versoes pv ON pv.id = p.versao_publicada_id "
                        "WHERE p.versao_publicada_id IS NOT NULL ORDER BY p.nome"
                    )
                )
            )
            .mappings()
            .all()
        )
    out = []
    for r in rows:
        definicao = r["definicao"] or {}
        execucao = definicao.get("execucao") if isinstance(definicao, dict) else None
        out.append(
            {
                "id": str(r["id"]),
                "nome": r["nome"],
                "sistema": bool(r["sistema"]),
                "execucao": execucao,
            }
        )
    return out


@router.post("/paginas/{pagina_id}/consultar")
async def consultar(pagina_id: uuid.UUID, dados: ConsultaIn, ator: uuid.UUID = Logado) -> dict:
    async with transacao() as conn:
        return await consulta.consultar_pagina(
            conn, pagina_id, filtros_extra=dados.filtros, execucao_id=dados.execucao_id
        )


@router.post("/paginas/{pagina_id}/exportar.xlsx")
async def exportar_pagina_xlsx(
    pagina_id: uuid.UUID, dados: ConsultaIn, ator: uuid.UUID = Logado
) -> Response:
    """Export the page query (on the fly, unstamped) as .xlsx, one sheet per block."""
    async with transacao() as conn:
        res = await consulta.consultar_pagina(
            conn, pagina_id, filtros_extra=dados.filtros, execucao_id=dados.execucao_id
        )
    conteudo = relatorio.blocos_para_xlsx(res["blocos"])
    return _resposta_xlsx(conteudo, f"pagina-{str(pagina_id)[:8]}.xlsx")


@router.post("/paginas/{pagina_id}/relatorio")
async def gerar_relatorio(
    pagina_id: uuid.UUID, dados: ConsultaIn, request: Request, ator: uuid.UUID = Logado
) -> dict:
    async with transacao() as conn:
        return await relatorio.gerar(
            conn,
            pagina_id,
            filtros=dados.filtros,
            gerado_por=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
            execucao_id=dados.execucao_id,
        )


@router.get("/relatorios")
async def listar_relatorios(ator: uuid.UUID = Logado) -> list[dict]:
    """Catalog of stamped reports (read-only, same authorization as the Portal query)."""
    async with transacao() as conn:
        return await relatorio.listar(conn)


@router.get("/relatorios/{relatorio_id}")
async def obter_relatorio(relatorio_id: uuid.UUID, ator: uuid.UUID = Logado) -> dict:
    async with transacao() as conn:
        return await relatorio.obter(conn, relatorio_id)


@router.get("/relatorios/{relatorio_id}/exportar.xlsx")
async def exportar_relatorio_xlsx(relatorio_id: uuid.UUID, ator: uuid.UUID = Logado) -> Response:
    """Export the stamped report (frozen blocks) as .xlsx, one sheet per block."""
    async with transacao() as conn:
        rel = await relatorio.obter(conn, relatorio_id)
    conteudo = relatorio.blocos_para_xlsx(rel["blocos"])
    return _resposta_xlsx(conteudo, f"relatorio-{str(relatorio_id)[:8]}.xlsx")
