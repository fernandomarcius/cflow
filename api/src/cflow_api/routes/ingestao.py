"""External ingestion routes: sources, runs, backfill/replay, quarantine and watermark.

Authorization at the edge: creating/editing/deleting a source and triggering backfill/replay are
**admin**-only, since they affect cadence, network cost and history reprocessing. Running and
reading are allowed for specialist and admin; auditor reads.

Temporal vocabulary does not appear here: callers see an "ingestion run", never a "workflow" or
"task queue".
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from cflow_core.authz.perfis import Perfil
from cflow_core.db import transacao
from cflow_core.errors import ErroAplicacao
from cflow_core.execucao import no_ingestao
from cflow_core.ingestao import repositorio as repo
from cflow_core.ingestao import service
from cflow_core.ingestao.contrato import validar
from cflow_core.request_context import request_id_atual

from ..deps import exigir_perfil

router = APIRouter(prefix="/ingestao")

SoAdmin = Depends(exigir_perfil(Perfil.ADMIN))
EspecialistaOuAdmin = Depends(exigir_perfil(Perfil.ESPECIALISTA, Perfil.ADMIN))
Leitura = Depends(exigir_perfil(Perfil.ESPECIALISTA, Perfil.ADMIN, Perfil.AUDITOR))


class CriarFonteIn(BaseModel):
    nome: str
    contrato: dict
    destino_conexao_id: uuid.UUID | None = None
    cadencia: str | None = None
    # Source maintained by the platform (seeded from the catalog), not hand-authored.
    sistema: bool = False


class EditarFonteIn(BaseModel):
    contrato: dict | None = None
    cadencia: str | None = None
    destino_conexao_id: uuid.UUID | None = None
    ativo: bool | None = None


class ExecutarIn(BaseModel):
    inicio: date | None = None
    fim: date | None = None


class ReprocessarIn(BaseModel):
    inicio: date
    fim: date


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# --- Sources --------------------------------------------------------------------------------


@router.post("/fontes", status_code=201)
async def criar_fonte(dados: CriarFonteIn, request: Request, ator: uuid.UUID = SoAdmin) -> dict:
    async with transacao() as conn:
        fid = await service.criar_fonte(
            conn,
            nome=dados.nome,
            contrato=dados.contrato,
            destino_conexao_id=dados.destino_conexao_id,
            cadencia=dados.cadencia,
            sistema=dados.sistema,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return {"id": str(fid)}


@router.get("/fontes")
async def listar_fontes(ator: uuid.UUID = Leitura) -> dict:
    async with transacao() as conn:
        return {"fontes": await repo.listar_fontes(conn)}


@router.get("/fontes/{fonte_id}")
async def obter_fonte(fonte_id: uuid.UUID, ator: uuid.UUID = Leitura) -> dict:
    async with transacao() as conn:
        fonte = await repo.carregar_fonte(conn, fonte_id)
    watermark = await repo.ler_watermark(fonte_id)
    return {
        **{k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in fonte.items()},
        "watermark": watermark,
    }


@router.patch("/fontes/{fonte_id}", status_code=204)
async def editar_fonte(
    fonte_id: uuid.UUID, dados: EditarFonteIn, request: Request, ator: uuid.UUID = SoAdmin
) -> Response:
    async with transacao() as conn:
        await service.editar_fonte(
            conn,
            fonte_id=fonte_id,
            contrato=dados.contrato,
            cadencia=dados.cadencia,
            destino_conexao_id=dados.destino_conexao_id,
            ativo=dados.ativo,
            ator_id=ator,
            ip=_ip(request),
            request_id=request_id_atual(),
        )
    return Response(status_code=204)


@router.delete("/fontes/{fonte_id}", status_code=204)
async def excluir_fonte(
    fonte_id: uuid.UUID, request: Request, ator: uuid.UUID = SoAdmin
) -> Response:
    async with transacao() as conn:
        await service.excluir_fonte(
            conn, fonte_id=fonte_id, ator_id=ator, ip=_ip(request), request_id=request_id_atual()
        )
    return Response(status_code=204)


@router.post("/fontes/validar")
async def validar_contrato(contrato: dict, ator: uuid.UUID = EspecialistaOuAdmin) -> dict:
    """Validate the contract WITHOUT registering it, the counterpart of "test connection":
    discovering that the encoding is missing after scheduling is discovering it too late."""
    validar(contrato)
    return {"ok": True}


@router.get("/fontes/{fonte_id}/estimativa")
async def estimar_fatias(
    fonte_id: uuid.UUID, inicio: date, fim: date, ator: uuid.UUID = SoAdmin
) -> dict:
    """What a backfill/replay WOULD request in this window, without touching the network: the "plan"
    the UI shows before enabling the click. Same function (`plano_dry_run`, via `contrato.fatias`)
    that the `ingestao` node preview and the real run use: the slice count returned is the count
    that will run.

    SoAdmin because it only feeds the two admin-only flows (backfill/replay); it is not general
    read access."""
    async with transacao() as conn:
        fonte = await repo.carregar_fonte(conn, fonte_id)
    watermark = await repo.ler_watermark(fonte_id)
    plano = no_ingestao.plano_dry_run(
        fonte=fonte, watermark=watermark, cfg={"inicio": inicio.isoformat(), "fim": fim.isoformat()}
    )
    return {
        "dataset": plano["dataset"],
        "classe": plano["classe"],
        "janela": plano["janela"],
        "fatias": len(plano["fatias"]),
    }


# --- Execution -------------------------------------------------------------------------------


@router.post("/fontes/{fonte_id}/executar")
async def executar(
    fonte_id: uuid.UUID,
    request: Request,
    dados: ExecutarIn | None = None,
    ator: uuid.UUID = EspecialistaOuAdmin,
) -> dict:
    """Incremental ingestion: from the watermark until today (or the given window).

    With the orchestrated engine, returns the run as 'executando'; follow it via
    GET /ingestao/execucoes/{id}. Without an orchestrator, runs inline and returns the totals."""
    return await service.executar(
        fonte_id=fonte_id,
        origem="manual",
        inicio=dados.inicio if dados else None,
        fim=dados.fim if dados else None,
        ator_id=ator,
        ip=_ip(request),
        request_id=request_id_atual(),
    )


@router.post("/fontes/{fonte_id}/backfill")
async def backfill(
    fonte_id: uuid.UUID, dados: ReprocessarIn, request: Request, ator: uuid.UUID = SoAdmin
) -> dict:
    """Redo the window **from the source**. Admin-only: a ten-year backfill is hundreds of requests
    to the public source, and that is how an IP gets blocked."""
    return await service.executar(
        fonte_id=fonte_id,
        origem="backfill",
        inicio=dados.inicio,
        fim=dados.fim,
        ator_id=ator,
        ip=_ip(request),
        request_id=request_id_atual(),
    )


@router.post("/fontes/{fonte_id}/replay")
async def replay(
    fonte_id: uuid.UUID, dados: ReprocessarIn, request: Request, ator: uuid.UUID = SoAdmin
) -> dict:
    """Rebuild bronze **from landing**, without touching the network. Used when the bronze rule
    changes (encoding, layout, expected column), not the source."""
    return await service.executar(
        fonte_id=fonte_id,
        origem="replay",
        inicio=dados.inicio,
        fim=dados.fim,
        replay=True,
        ator_id=ator,
        ip=_ip(request),
        request_id=request_id_atual(),
    )


@router.get("/execucoes")
async def listar_execucoes(
    fonte_id: uuid.UUID | None = None,
    limite: int = Query(default=50, ge=1, le=500),
    ator: uuid.UUID = Leitura,
) -> dict:
    async with transacao() as conn:
        return {"execucoes": await repo.listar_execucoes(conn, fonte_id=fonte_id, limite=limite)}


@router.get("/execucoes/{execucao_id}")
async def consultar_execucao(execucao_id: uuid.UUID, ator: uuid.UUID = Leitura) -> dict:
    async with transacao() as conn:
        return await repo.consultar_execucao(conn, execucao_id)


@router.get("/execucoes/{execucao_id}/fatias")
async def listar_fatias(execucao_id: uuid.UUID, ator: uuid.UUID = Leitura) -> dict:
    """The run LOG: one row per slice, with outcome, attempts, duration and reason.

    This is what the aggregate summary never answered. A 31-day window that returned "22 batches"
    may be 22 good + 9 weekend days (nothing wrong) or 22 good + 9 lost (a hole in the data), and
    the two cases were indistinguishable by looking at the run."""
    async with transacao() as conn:
        await repo.consultar_execucao(conn, execucao_id)  # 404 before listing
        return {"fatias": await repo.listar_fatias(conn, execucao_id)}


@router.post("/execucoes/{execucao_id}/retentar")
async def retentar(
    execucao_id: uuid.UUID, request: Request, ator: uuid.UUID = EspecialistaOuAdmin
) -> dict:
    """Redo **only the slices that ended badly** in this run, like a scheduler's "clear task".

    Specialist, not admin as with backfill, because the set is CLOSED: the caller does not choose
    the window, the platform picks among what it itself recorded as failed. The network cost stays
    bounded by the original run's: the asymmetry is deliberate (a reload click costs one window; a
    backfill click costs our IP's reputation at the source)."""
    return await service.retentar(
        execucao_id=execucao_id,
        ator_id=ator,
        ip=_ip(request),
        request_id=request_id_atual(),
    )


@router.get("/fontes/{fonte_id}/fatias/{chave_lote}")
async def historico_da_fatia(
    fonte_id: uuid.UUID,
    chave_lote: str,
    limite: int = Query(default=50, ge=1, le=200),
    ator: uuid.UUID = Leitura,
) -> dict:
    """Every time THIS slice ran, in any run: "since when has the 2026-08-03 slice been
    quarantined?". A single run's log only answers for that run."""
    async with transacao() as conn:
        await repo.carregar_fonte(conn, fonte_id)  # 404 before listing
        return {
            "fatias": await repo.historico_da_fatia(
                conn, fonte_id=fonte_id, chave_lote=chave_lote, limite=limite
            )
        }


@router.get("/quarentena/resumo")
async def resumo_quarentena(
    fonte_id: uuid.UUID | None = None,
    ator: uuid.UUID = Leitura,
) -> dict:
    """Quarantine aggregated per source: who has to look, and what changes the decision.

    `GET /ingestao/quarentena` returns raw rows; nobody opens 500 rows to find out that five
    sources have been quarantined for months. Here, per source: `total`, `ultimos_7d`,
    `ultimos_30d`, `primeiro_em`, `ultimo_em`, the three most frequent `codigos` and
    **`fatias_sem_lote_bom`**, the count of slices that fell into quarantine and never landed in
    bronze.

    That last field changes the decision: a quarantine whose slice later landed is already-resolved
    noise; a quarantine with no good batch is data that **does not exist**, and silver comes out
    consistent with bronze but without the missing period, invisible to every guard that compares
    the two layers.

    Pure read: writes nothing and therefore does not write to the audit trail (auditing is for
    writes)."""
    async with transacao() as conn:
        if fonte_id is not None:
            await repo.carregar_fonte(conn, fonte_id)  # 404 before summarizing
        return {"fontes": await repo.resumo_quarentena(conn, fonte_id=fonte_id)}


@router.get("/quarentena")
async def listar_quarentena(
    fonte_id: uuid.UUID | None = None,
    limite: int = Query(default=100, ge=1, le=500),
    ator: uuid.UUID = Leitura,
) -> dict:
    """The queryable DLQ: what failed, at which stage, and where the raw payload was kept."""
    async with transacao() as conn:
        return {"itens": await repo.listar_quarentena(conn, fonte_id=fonte_id, limite=limite)}


@router.get("/fontes/{fonte_id}/lotes")
async def listar_lotes(fonte_id: uuid.UUID, ator: uuid.UUID = Leitura) -> dict:
    """Batches already landed (one per slice, the most recent). The index of what exists in the lake."""
    async with transacao() as conn:
        await repo.carregar_fonte(conn, fonte_id)  # 404 before listing
    return {"lotes": await repo.lotes_da_fonte(fonte_id)}


@router.get("/fontes/{fonte_id}/manifesto/{chave_lote}")
async def obter_manifesto(fonte_id: uuid.UUID, chave_lote: str, ator: uuid.UUID = Leitura) -> dict:
    """Manifest of a slice: the provenance proof (bytes, hash, origin, contract, timestamp)."""
    lotes = await repo.lotes_da_fonte(fonte_id, chaves=[chave_lote])
    if not lotes:
        raise ErroAplicacao(
            codigo="nao_encontrado", mensagem="No batch for this slice.", status=404
        )
    return lotes[0].get("manifesto") or {}
