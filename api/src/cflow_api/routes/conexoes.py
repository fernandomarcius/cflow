"""Connection and credential (vault) routes. The secret enters only in the request body (secure
channel), goes to the vault and never comes back. Writes are audited (no secret in the diff).
Authorization at the edge.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from cflow_core.authz.perfis import Perfil
from cflow_core.conexoes import service
from cflow_core.conexoes.diagnostico import causa_consulta
from cflow_core.db import transacao
from cflow_core.errors import DetalheErro, ErroAplicacao
from cflow_core.request_context import request_id_atual

from ..deps import _perfil_de, exigir_perfil, usuario_atual

router = APIRouter()
EspecialistaOuAdmin = Depends(exigir_perfil(Perfil.ESPECIALISTA, Perfil.ADMIN))
SoAdmin = Depends(exigir_perfil(Perfil.ADMIN))


class CriarConexaoIn(BaseModel):
    nome: str
    tipo: str
    config: dict
    segredo: str | None = None  # writeOnly: goes to the vault, never returned


class RotacaoIn(BaseModel):
    novo_segredo: str


class EscritaIn(BaseModel):
    permitir_escrita: bool


class RenomearConexaoIn(BaseModel):
    nome: str


class EditarConexaoIn(BaseModel):
    config: dict
    # writeOnly: if absent (None) the current credential is KEPT; if given, it is rotated/stored
    # in the vault.
    credencial: str | None = None


class AmostraIn(BaseModel):
    sql: str | None = (
        None  # empty/None for a file (reads the whole sheet); or a SELECT for a database/gateway
    )
    limite: int | None = 2000


class OpcoesIn(BaseModel):
    # Dropdown options (operator filter): value sent + label displayed.
    sql: str | None = None  # SELECT (DB/gateway) or empty (file: reads everything)
    coluna_valor: str  # VALUE column (what the param receives, e.g. customer_id)
    coluna_rotulo: str | None = None  # displayed LABEL column (e.g. customer_name); absent = value
    limite: int | None = 2000


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/conexoes", status_code=201)
async def criar_conexao(
    dados: CriarConexaoIn, request: Request, ator: uuid.UUID = EspecialistaOuAdmin
) -> dict:
    async with transacao() as conn:
        cid = await service.criar(
            conn,
            nome=dados.nome,
            tipo=dados.tipo,
            config=dados.config,
            segredo=dados.segredo,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"id": str(cid)}


@router.post("/conexoes/{conexao_id}/amostra")
async def amostra_conexao(
    conexao_id: uuid.UUID, dados: AmostraIn, ator: uuid.UUID = EspecialistaOuAdmin
) -> dict:
    """Read a sample from the connection (e.g. the front end reads a `arquivo` map to build
    dropdowns). Replaces the hack of calling a dummy pipeline's preview just to read a connection.

    A query error is a 400 carrying the server-side cause in the detail, not a generic 500: the
    caller wrote the SQL, and "An internal error occurred" sent them hunting for a platform bug."""
    try:
        colunas, linhas = await service.amostra(conexao_id, dados.sql or "", dados.limite)
    except ErroAplicacao:
        raise
    except Exception as exc:  # noqa: BLE001 - cause sanitised by allow-list (diagnostico)
        causa = causa_consulta(exc)
        if causa is None:
            raise
        raise ErroAplicacao(
            codigo="erro_consulta",
            mensagem="The query failed at the source.",
            status=400,
            detalhes=[DetalheErro(campo="sql", mensagem=causa)],
        ) from exc
    return {"colunas": colunas, "linhas": linhas}


@router.post("/conexoes/{conexao_id}/opcoes")
async def opcoes_conexao(
    conexao_id: uuid.UUID, dados: OpcoesIn, ator: uuid.UUID = Depends(usuario_atual)
) -> list[dict]:
    """Dynamic options for a filter dropdown: DISTINCT `[{valor, rotulo}]`, sorted by label.
    Reads the connection (SELECT for DB/gateway, or the whole sheet for a file) and projects value
    + label. Available to any authenticated user (the operator needs it to fill in the filter).
    Read-only."""

    def _pega(linha: dict, nome: str):
        if nome in linha:
            return linha[nome]
        alvo = nome.lower().strip()
        for k, v in linha.items():
            if str(k).lower().strip() == alvo:
                return v
        return None

    # The source is an external system: it goes down, gets slow or rejects the credential, and
    # that is NOT an internal platform error. Without this handling the connector timeout surfaced
    # as `erro_nao_tratado` (500 + traceback in the log); the right answer is the error envelope,
    # which the UI uses to tell the operator the list did not load and to free up the free-text
    # field.
    try:
        _cols, linhas = await service.amostra(conexao_id, dados.sql or "", dados.limite)
    except ErroAplicacao:
        raise
    except (TimeoutError, OSError, ConnectionError) as e:
        raise ErroAplicacao(
            "fonte_indisponivel",
            "The source did not respond in time; the option list could not be loaded.",
            502,
        ) from e
    except (
        Exception
    ) as e:  # the connector driver raises its own exception (pyodbc, httpx, duckdb...)
        raise ErroAplicacao(
            "fonte_indisponivel",
            "Could not query the source to build the option list.",
            502,
        ) from e
    rotcol = dados.coluna_rotulo or dados.coluna_valor
    vistos: set[str] = set()
    out: list[dict] = []
    for linha in linhas:
        valor = str(_pega(linha, dados.coluna_valor) or "").strip()
        if not valor or valor in vistos:
            continue
        vistos.add(valor)
        rotulo = str(_pega(linha, rotcol) or "").strip() or valor
        out.append({"valor": valor, "rotulo": rotulo})

    def _chave(o: dict):
        # Numeric value -> NUMERIC order (1,2,3,...,10,11), not alphabetical ("10" before "2").
        # Non-numeric -> alphabetical by label.
        try:
            return (0, float(o["valor"]), "")
        except (TypeError, ValueError):
            return (1, 0.0, o["rotulo"].lower())

    out.sort(key=_chave)
    return out


@router.get("/conexoes")
async def listar_conexoes(ator: uuid.UUID = EspecialistaOuAdmin) -> list[dict]:
    async with transacao() as conn:
        return await service.listar(conn)


@router.delete("/conexoes/{conexao_id}", status_code=204)
async def excluir_conexao(
    conexao_id: uuid.UUID, request: Request, ator: uuid.UUID = EspecialistaOuAdmin
) -> Response:
    perfil = await _perfil_de(ator)
    async with transacao() as conn:
        await service.excluir(
            conn,
            conexao_id=conexao_id,
            ator_id=ator,
            ator_perfil=perfil,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return Response(status_code=204)


@router.patch("/conexoes/{conexao_id}")
async def renomear_conexao(
    conexao_id: uuid.UUID,
    dados: RenomearConexaoIn,
    request: Request,
    ator: uuid.UUID = EspecialistaOuAdmin,
) -> dict:
    perfil = await _perfil_de(ator)
    async with transacao() as conn:
        return await service.renomear(
            conn,
            conexao_id=conexao_id,
            nome=dados.nome,
            ator_id=ator,
            ator_perfil=perfil,
            ip=_ip(request),
            request_id=request_id_atual(),
        )


class DuplicarConexaoIn(BaseModel):
    nome: str


@router.post("/conexoes/{conexao_id}/duplicar", status_code=201)
async def duplicar_conexao(
    conexao_id: uuid.UUID,
    dados: DuplicarConexaoIn,
    request: Request,
    ator: uuid.UUID = EspecialistaOuAdmin,
) -> dict:
    """New connection with the type, config and the SAME credential as the source, without the
    secret ever travelling (the encrypted row is copied inside the vault). Creator or admin of the
    source; the copy is born with writes disabled."""
    perfil = await _perfil_de(ator)
    async with transacao() as conn:
        cid = await service.duplicar(
            conn,
            conexao_id=conexao_id,
            nome=dados.nome,
            ator_id=ator,
            ator_perfil=perfil,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"id": str(cid)}


@router.put("/conexoes/{conexao_id}", status_code=204)
async def editar_conexao(
    conexao_id: uuid.UUID,
    dados: EditarConexaoIn,
    request: Request,
    ator: uuid.UUID = EspecialistaOuAdmin,
) -> Response:
    perfil = await _perfil_de(ator)
    async with transacao() as conn:
        await service.editar(
            conn,
            conexao_id=conexao_id,
            config=dados.config,
            segredo=dados.credencial,
            ator_id=ator,
            ator_perfil=perfil,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return Response(status_code=204)


@router.post("/conexoes/{conexao_id}/testar")
async def testar_conexao(conexao_id: uuid.UUID, ator: uuid.UUID = EspecialistaOuAdmin) -> dict:
    # No `transacao()` here: the service opens its own short one just to resolve the credential
    # and talks to the source outside it. The source is a third-party system and may take minutes.
    return await service.testar(conexao_id)


@router.get("/conexoes/{conexao_id}/schema")
async def schema_conexao(conexao_id: uuid.UUID, ator: uuid.UUID = EspecialistaOuAdmin) -> dict:
    return await service.navegar_schema(conexao_id)


@router.get("/conexoes/{conexao_id}/colunas")
async def colunas_conexao(
    conexao_id: uuid.UUID, schema: str, tabela: str, ator: uuid.UUID = EspecialistaOuAdmin
) -> list[dict]:
    return await service.colunas(conexao_id, schema, tabela)


@router.patch("/conexoes/{conexao_id}/escrita")
async def definir_escrita_conexao(
    conexao_id: uuid.UUID, dados: EscritaIn, request: Request, ator: uuid.UUID = SoAdmin
) -> dict:
    """Toggle `permitir_escrita` (enables effect nodes). Admin-only: non-admin gets a generic 403
    via `SoAdmin`. Audited (before/after diff)."""
    async with transacao() as conn:
        return await service.definir_escrita(
            conn,
            conexao_id=conexao_id,
            permitir=dados.permitir_escrita,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )


@router.post("/conexoes/{conexao_id}/rotacionar-credencial", status_code=204)
async def rotacionar(
    conexao_id: uuid.UUID, dados: RotacaoIn, request: Request, ator: uuid.UUID = SoAdmin
) -> Response:
    async with transacao() as conn:
        await service.rotacionar_credencial(
            conn,
            conexao_id=conexao_id,
            novo_segredo=dados.novo_segredo,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return Response(status_code=204)
