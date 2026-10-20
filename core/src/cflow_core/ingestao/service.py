"""Ingestion service: (audited) source registry and run orchestration.

Three run modes, same per-slice mechanics:
  - **incremental** — from the watermark up to today (what the scheduler triggers);
  - **backfill**    — explicit window, redoing from the ORIGIN;
  - **replay**      — explicit window, rebuilding bronze from LANDING (no network).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import date, timedelta
from functools import partial

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from ..audit import Acao, registrar
from ..config import get_settings
from ..db import transacao
from ..errors import DetalheErro, ErroAplicacao
from ..timezone import FUSO_SAO_PAULO, agora_utc, para_exibicao
from . import armazenamento, motor
from . import repositorio as repo
from .contrato import CADENCIAS, Fatia, fatias, limite, piso_data, validar

# `retentativa` (retry) redoes only the slices that ended badly in a previous run; `pipeline` is the
# ingestion running as a node of a Studio graph. Both stay DISTINCT from `manual` on purpose: the
# origin answers "who triggered this?" in the history, and lumping everything under "manual" would
# erase the difference between a click, a retry and a pipeline run.
ORIGENS = ("manual", "agendada", "backfill", "replay", "retentativa", "pipeline")

# The event name is a CONTRACT (the key the log metric filters on), not free text.
logger = logging.getLogger("cflow.ingestao.service")

# Cadence -> interval until the next automatic trigger. `manual` is never scheduled.
INTERVALO = {
    "diaria": timedelta(days=1),
    "semanal": timedelta(days=7),
    "mensal": timedelta(days=30),
    "anual": timedelta(days=365),
}


def _json(d) -> str:
    return json.dumps(d, ensure_ascii=False)


def hoje_local() -> date:
    """Today in the operator's local timezone. The `ingestion_date` partition belongs to the
    operator's day, not UTC's; otherwise anything running in the evening would land in the next
    day's partition."""
    return para_exibicao(agora_utc(), FUSO_SAO_PAULO).date()


# --- Source registry (audited) -----------------------------------------------------------


async def criar_fonte(
    conn: AsyncConnection,
    *,
    nome: str,
    contrato: dict,
    destino_conexao_id: uuid.UUID | None,
    cadencia: str | None,
    ator_id: uuid.UUID,
    sistema: bool = False,
    ip: str | None = None,
    request_id: str | None = None,
) -> uuid.UUID:
    validar(contrato)
    cadencia = cadencia or contrato.get("cadencia") or "manual"
    if cadencia not in CADENCIAS:
        raise ErroAplicacao(codigo="cadencia_invalida", mensagem="Invalid cadence.", status=400)
    nome = (nome or "").strip()
    if not nome:
        raise ErroAplicacao(codigo="nome_invalido", mensagem="Provide a name.", status=400)

    existe = (
        await conn.execute(
            text("SELECT 1 FROM ingestao_fontes WHERE nome = :n OR dataset = :d"),
            {"n": nome, "d": contrato["dataset"]},
        )
    ).first()
    if existe is not None:
        raise ErroAplicacao(
            codigo="fonte_em_uso",
            mensagem="A source with this name or dataset already exists.",
            status=409,
        )
    await _exigir_destino_objeto(conn, destino_conexao_id)

    fid = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO ingestao_fontes (id, nome, dataset, classe, contrato, destino_conexao_id, "
            "cadencia, proxima_em, criado_por, sistema) VALUES (:id, :n, :d, :c, "
            "CAST(:ct AS jsonb), :dc, :cad, :prox, :por, :sis)"
        ),
        {
            "id": fid,
            "n": nome,
            "d": contrato["dataset"],
            "c": contrato["classe"],
            "ct": _json(contrato),
            "dc": destino_conexao_id,
            "cad": cadencia,
            "prox": agora_utc() if cadencia != "manual" else None,
            "por": ator_id,
            "sis": sistema,
        },
    )
    await registrar(
        conn,
        acao=Acao.CRIAR,
        recurso_tipo="ingestao_fonte",
        recurso_id=fid,
        ator_id=ator_id,
        antes=None,
        depois={"nome": nome, "cadencia": cadencia, "sistema": sistema, "contrato": contrato},
        request_id=request_id,
        ip=ip,
    )
    return fid


async def _exigir_destino_objeto(
    conn: AsyncConnection, destino_conexao_id: uuid.UUID | None
) -> None:
    """The destination, when given, must be an `objeto` (object storage) connection. Pointing the
    lake at a database connection would only fail on the first write, after everything was already
    downloaded."""
    if destino_conexao_id is None:
        return
    tipo = (
        await conn.execute(
            text("SELECT tipo FROM conexoes WHERE id = :id"), {"id": destino_conexao_id}
        )
    ).scalar_one_or_none()
    if tipo is None:
        raise ErroAplicacao(
            codigo="nao_encontrado", mensagem="Destination connection not found.", status=404
        )
    if str(tipo) != "objeto":
        raise ErroAplicacao(
            codigo="destino_invalido",
            mensagem=f"The destination connection is of type '{tipo}'; the lake requires an 'objeto' connection.",
            status=400,
        )


async def editar_fonte(
    conn: AsyncConnection,
    *,
    fonte_id: uuid.UUID,
    contrato: dict | None = None,
    cadencia: str | None = None,
    destino_conexao_id: uuid.UUID | None = None,
    ativo: bool | None = None,
    ator_id: uuid.UUID,
    ip: str | None = None,
    request_id: str | None = None,
) -> None:
    atual = await repo.carregar_fonte(conn, fonte_id)
    antes = {
        "contrato": atual["contrato"],
        "cadencia": atual["cadencia"],
        "ativo": atual["ativo"],
        "destino_conexao_id": (
            str(atual["destino_conexao_id"]) if atual["destino_conexao_id"] else None
        ),
    }
    sets: list[str] = []
    params: dict = {"id": fonte_id}

    if contrato is not None:
        validar(contrato)
        if contrato["dataset"] != atual["dataset"]:
            # The dataset is the path in the lake. Changing it would orphan the history already
            # landed: the source would point at an empty folder while the watermark referred to
            # another place.
            raise ErroAplicacao(
                codigo="dataset_imutavel",
                mensagem="The 'dataset' cannot change: it is the path of the data already written to "
                "the lake. Create another source.",
                status=409,
            )
        sets.append("contrato = CAST(:ct AS jsonb)")
        sets.append("classe = :cl")
        params["ct"] = _json(contrato)
        params["cl"] = contrato["classe"]
    if cadencia is not None:
        if cadencia not in CADENCIAS:
            raise ErroAplicacao(codigo="cadencia_invalida", mensagem="Invalid cadence.", status=400)
        sets.append("cadencia = :cad")
        params["cad"] = cadencia
        sets.append(
            "proxima_em = CASE WHEN :cad = 'manual' THEN NULL ELSE COALESCE(proxima_em, now()) END"
        )
    if destino_conexao_id is not None:
        await _exigir_destino_objeto(conn, destino_conexao_id)
        sets.append("destino_conexao_id = :dc")
        params["dc"] = destino_conexao_id
    if ativo is not None:
        sets.append("ativo = :at")
        params["at"] = ativo
    if not sets:
        return

    sets.append("atualizado_em = now()")
    await conn.execute(text(f"UPDATE ingestao_fontes SET {', '.join(sets)} WHERE id = :id"), params)
    depois = {
        "contrato": contrato if contrato is not None else atual["contrato"],
        "cadencia": cadencia if cadencia is not None else atual["cadencia"],
        "ativo": ativo if ativo is not None else atual["ativo"],
        "destino_conexao_id": (
            str(destino_conexao_id) if destino_conexao_id else antes["destino_conexao_id"]
        ),
    }
    await registrar(
        conn,
        acao=Acao.EDITAR,
        recurso_tipo="ingestao_fonte",
        recurso_id=fonte_id,
        ator_id=ator_id,
        antes=antes,
        depois=depois,
        request_id=request_id,
        ip=ip,
    )


async def excluir_fonte(
    conn: AsyncConnection,
    *,
    fonte_id: uuid.UUID,
    ator_id: uuid.UUID,
    ip: str | None = None,
    request_id: str | None = None,
) -> None:
    """Deletes the source and its run metadata (CASCADE). It does **not** delete the lake: landing
    and bronze are immutable and stay there; the ingested data belongs to the organization, not to
    the registry."""
    atual = await repo.carregar_fonte(conn, fonte_id)
    await conn.execute(text("DELETE FROM ingestao_fontes WHERE id = :id"), {"id": fonte_id})
    await registrar(
        conn,
        acao=Acao.EXCLUIR,
        recurso_tipo="ingestao_fonte",
        recurso_id=fonte_id,
        ator_id=ator_id,
        antes={"nome": atual["nome"], "dataset": atual["dataset"]},
        depois=None,
        request_id=request_id,
        ip=ip,
    )


# --- Window ---------------------------------------------------------------------------------


def _inicio_da_chave(chave: str) -> date | None:
    base = chave.split("..")[0]
    return piso_data(base)


def resolver_janela(
    contrato: dict, watermark: dict | None, inicio: date | None, fim: date | None
) -> tuple[date, date]:
    """Effective window. Without an explicit `inicio`, it starts at the WATERMARK and
    **reprocesses its slice** on purpose: the most recent slice is usually the one the origin still
    republishes (the current month of a registry changes daily). Reprocessing is cheap (an equal
    hash becomes a no-op); skipping loses data."""
    fim = fim or hoje_local()
    if inicio is None:
        if watermark and (d := _inicio_da_chave(watermark["chave_lote"])):
            inicio = d
        else:
            inicio = piso_data((contrato.get("janela") or {}).get("inicio")) or fim
    if inicio > fim:
        raise ErroAplicacao(
            codigo="janela_invalida",
            mensagem="The window start date is after its end date.",
            status=400,
        )
    return inicio, fim


def fatias_json(lista: list[Fatia]) -> list[dict]:
    """Serialization to cross Temporal (the workflow only exchanges JSON)."""
    return [
        {"chave": f.chave, "inicio": f.inicio.isoformat(), "fim": f.fim.isoformat()} for f in lista
    ]


def fatia_de_json(d: dict) -> Fatia:
    return Fatia(
        chave=d["chave"],
        inicio=date.fromisoformat(d["inicio"]),
        fim=date.fromisoformat(d["fim"]),
    )


# --- Run -------------------------------------------------------------------------------


async def preparar(
    *,
    fonte_id: uuid.UUID,
    origem: str,
    inicio: date | None,
    fim: date | None,
    ator_id: uuid.UUID | None,
    ip: str | None = None,
    request_id: str | None = None,
    fatias_forcadas: list[dict] | None = None,
) -> dict:
    """Creates the run and records it in the audit trail (same transaction). Returns the context
    the workflow (or the inline path) needs: source, slices and run id.

    `fatias_forcadas` replaces the window cut with an ALREADY decided list; the retry uses it. It
    does not come from the API caller: it comes from `repo.fatias_retentaveis`, i.e. from what the
    platform itself recorded as unsuccessful. Accepting arbitrary slices here would turn a retry
    (specialist role) into a backfill (admin role) through the back door."""
    if origem not in ORIGENS:
        raise ErroAplicacao(codigo="origem_invalida", mensagem="Invalid run origin.", status=400)
    # One in-flight run per source (enforced by a partial unique index). Ask Temporal before
    # refusing: a run whose workflow died is closed here and does not block the next one.
    em_voo = await execucao_em_voo(fonte_id)
    if em_voo is not None:
        raise _erro_em_andamento(em_voo)
    try:
        return await _preparar_ou_falhar_no_unique(
            fonte_id=fonte_id,
            origem=origem,
            inicio=inicio,
            fim=fim,
            ator_id=ator_id,
            ip=ip,
            request_id=request_id,
            fatias_forcadas=fatias_forcadas,
        )
    except IntegrityError as e:
        # Two triggers both passed the pre-check: the partial index decided. The loser gets the
        # same 409, pointing at the winner.
        if NOME_UNIQUE_EM_VOO not in str(getattr(e, "orig", e)):
            raise
        em_voo = await execucao_em_voo(fonte_id)
        raise _erro_em_andamento(em_voo or {"id": None}) from e


NOME_UNIQUE_EM_VOO = "ux_ingestao_execucoes_em_voo"


async def execucao_em_voo(fonte_id: uuid.UUID) -> dict | None:
    """This source's `executando` (running) run, if it is really still running.

    Same discipline as the pipeline guard (`engine.execucao_em_voo`): ALWAYS ask Temporal, close on
    the spot the one that is not `RUNNING` (`falhar`, the same path as the activity), and without a
    verdict (network, inline) the row COUNTS. Ingestion has no reaper of its own; without this check
    an orphaned run would block the source until someone intervened."""
    async with transacao() as conn:
        linha = (
            await conn.execute(
                text(
                    "SELECT id, iniciada_em FROM ingestao_execucoes "
                    "WHERE fonte_id = :f AND status = 'executando'"
                ),
                {"f": fonte_id},
            )
        ).first()
    if linha is None:
        return None
    em_voo = {"id": linha[0], "iniciada_em": linha[1]}
    cfg = get_settings()
    if not cfg.usar_temporal:
        return em_voo
    from ..execucao import reconciliacao

    try:
        cliente = await reconciliacao.conectar(cfg.temporal_host)
        veredito = await reconciliacao.consultar_uma(cliente, linha[0], prefixos=("ingestao-",))
    except Exception:  # noqa: BLE001 — orchestrator silence is not a verdict
        return em_voo
    if veredito.viva:
        return em_voo
    await falhar(
        execucao_id=linha[0],
        erro=veredito.detalhe or reconciliacao.MENSAGEM_ORFA,
        contagens={},
        fonte_id=fonte_id,
    )
    return None


def _erro_em_andamento(em_voo: dict) -> ErroAplicacao:
    detalhes = (
        [DetalheErro(campo="execucao_id", mensagem=str(em_voo["id"]))] if em_voo.get("id") else []
    )
    return ErroAplicacao(
        codigo="ingestao_em_andamento",
        mensagem="A run of this source is already in progress.",
        status=409,
        detalhes=detalhes,
    )


async def _preparar_ou_falhar_no_unique(
    *,
    fonte_id: uuid.UUID,
    origem: str,
    inicio: date | None,
    fim: date | None,
    ator_id: uuid.UUID | None,
    ip: str | None,
    request_id: str | None,
    fatias_forcadas: list[dict] | None,
) -> dict:
    """The original body of `preparar`: creates the run and records it in the audit trail (same transaction)."""
    async with transacao() as conn:
        fonte = await repo.carregar_fonte(conn, fonte_id)
        if not fonte["ativo"] and origem == "agendada":
            raise ErroAplicacao(codigo="fonte_inativa", mensagem="Source is inactive.", status=409)
        contrato = fonte["contrato"]
        validar(contrato)
        watermark = (
            (
                await conn.execute(
                    text("SELECT chave_lote FROM ingestao_watermarks WHERE fonte_id = :f"),
                    {"f": fonte_id},
                )
            )
            .mappings()
            .first()
        )
        ini, f = resolver_janela(contrato, dict(watermark) if watermark else None, inicio, fim)
        if fatias_forcadas is not None:
            lista = [fatia_de_json(d) for d in fatias_forcadas]
            # The recorded window is that of the slices about to run, not the one the watermark
            # would suggest: a retry of two July slices recorded with the window "July..today"
            # would misstate what it covered.
            if lista:
                ini, f = min(x.inicio for x in lista), max(x.fim for x in lista)
        else:
            lista = fatias(contrato, ini, f)
        execucao_id = await repo.criar_execucao(
            conn,
            fonte_id=fonte_id,
            origem=origem,
            janela_inicio=ini,
            janela_fim=f,
            criado_por=ator_id,
        )
        await registrar(
            conn,
            acao=Acao.EXECUTAR,
            recurso_tipo="ingestao_fonte",
            recurso_id=fonte_id,
            ator_id=ator_id,
            antes=None,
            depois={
                "execucao_id": str(execucao_id),
                "origem": origem,
                "janela": {"inicio": ini.isoformat(), "fim": f.isoformat()},
                "fatias": len(lista),
                "dataset": fonte["dataset"],
            },
            request_id=request_id,
            ip=ip,
        )
    logger.info(
        "ingestao_execucao_iniciada",
        extra={
            "extra_campos": {
                "fonte": str(fonte_id),
                "dataset": fonte["dataset"],
                "execucao_id": str(execucao_id),
                "origem": origem,
                "janela_inicio": ini.isoformat(),
                "janela_fim": f.isoformat(),
                "fatias": len(lista),
                # `_disparar` already maps replay to the origin; deriving it here avoids a
                # parameter that would exist only for the log and could diverge from what was
                # recorded on the run.
                "replay": origem == "replay",
            }
        },
    )
    return {
        "execucao_id": str(execucao_id),
        "fonte_id": str(fonte_id),
        "dataset": fonte["dataset"],
        "janela": {"inicio": ini.isoformat(), "fim": f.isoformat()},
        "fatias": fatias_json(lista),
        # The workflow is deterministic and does not read the database: concurrency travels with the context.
        "concorrencia": max(1, limite(contrato, "concorrencia", 4)),
    }


async def _lake_da_fonte(fonte: dict):
    return await armazenamento.abrir(fonte.get("destino_conexao_id"))


async def _com_log(
    *,
    fonte_id: uuid.UUID | str,
    execucao_id: uuid.UUID | str,
    fatia_ctx: dict,
    corpo,
    dataset: str | None = None,
    metricas: dict | None = None,
    tentativa: int | None = None,
) -> dict:
    """Runs the body of ONE slice, timing it and recording the outcome in `ingestao_fatias`.

    It wraps both paths (Temporal activity and inline) because this is the record that was missing
    to answer "what happened to each slice?", the question the run's aggregate summary cannot
    answer and the reason someone opens the screen.

    INFRA failure (the only kind that bubbles up here: the engine turns data failure into
    quarantine) is recorded as `falha` **and re-raised**, since Temporal must see it to retry.
    Without recording before re-raising, a slice that blew up three times would leave no trace."""
    t0 = time.monotonic()
    inicio = fatia_ctx.get("inicio")
    fim = fatia_ctx.get("fim")
    comum = {
        "execucao_id": execucao_id,
        "fonte_id": fonte_id,
        "chave_lote": fatia_ctx["chave"],
        "janela_inicio": date.fromisoformat(inicio) if inicio else None,
        "janela_fim": date.fromisoformat(fim) if fim else None,
    }
    identificacao = {
        "fonte": str(fonte_id),
        "dataset": dataset,
        "execucao_id": str(execucao_id),
        "chave_lote": fatia_ctx["chave"],
        # The REAL attempt number is the Temporal activity's (`activity.info().attempt`), which
        # this module cannot see. It comes as a parameter from whoever knows; `None` means
        # "unknown", which is honest: a fixed `1` would make every retry look like a first run.
        "tentativa": tentativa,
    }
    try:
        contagens = await corpo()
    except Exception as exc:  # noqa: BLE001 — record and re-raise; the caller decides whether to retry
        duracao_ms = int((time.monotonic() - t0) * 1000)
        await repo.registrar_fatia(
            **comum,
            contagens={},
            desfecho="falha",
            erro=_mensagem_infra(exc),
            duracao_ms=duracao_ms,
        )
        logger.warning(
            "ingestao_fatia_falhou",
            extra={
                "extra_campos": {
                    **identificacao,
                    "desfecho": "falha",
                    "linhas": (metricas or {}).get("linhas", 0),
                    "bytes": (metricas or {}).get("bytes", 0),
                    "duracao_ms": duracao_ms,
                    "codigo": exc.codigo if isinstance(exc, ErroAplicacao) else "erro_infra",
                    # `motivo`, not `mensagem`: `extra_campos` overwrites the formatter's
                    # field, and the event name lives in `mensagem`.
                    "motivo": motor.sanear_texto(_mensagem_infra(exc))[:200],
                }
            },
        )
        raise
    duracao_ms = int((time.monotonic() - t0) * 1000)
    await repo.registrar_fatia(**comum, contagens=contagens or {}, duracao_ms=duracao_ms)
    logger.info(
        "ingestao_fatia_concluida",
        extra={
            "extra_campos": {
                **identificacao,
                "desfecho": repo.desfecho_de(contagens or {}),
                "linhas": (metricas or {}).get("linhas", 0),
                "bytes": (metricas or {}).get("bytes", 0),
                "duracao_ms": duracao_ms,
            }
        },
    )
    return contagens


def _mensagem_infra(exc: Exception) -> str:
    """Infra failure message, with no stack trace and no secret (error envelope rule).

    The CLASS name is included because `ConnectionError` and `TimeoutError` call for opposite
    remedies, and "Ingestion failed." repeated across many log lines helps nobody. A class name
    carries no value: URL, credentials and payload stay out."""
    if isinstance(exc, ErroAplicacao):
        return exc.mensagem
    return f"Infrastructure failure ({type(exc).__name__})."


async def _replay_dos_lotes(
    fonte: dict, execucao_id, lotes: list[dict], lake, metricas: dict | None = None
) -> dict:
    """Rebuilds bronze for all batches of ONE slice. A named function (not a closure in the loop)
    because `lotes` must be captured per slice; a closure over the loop variable would give every
    slice the last value."""
    parcial: dict = {}
    for lote in lotes:
        _acumular(
            parcial,
            await motor.reprocessar_fatia(
                fonte=fonte, execucao_id=execucao_id, lote=lote, lake=lake, metricas=metricas
            ),
        )
    return parcial


async def processar_uma_fatia(
    *,
    fonte_id: uuid.UUID | str,
    execucao_id: uuid.UUID | str,
    fatia_ctx: dict,
    replay: bool = False,
) -> dict:
    """ONE slice, the unit of the Temporal activity (and therefore of retry and of
    resume-from-failure replay). Returns the partial counts."""
    fonte = await repo.obter_fonte(fonte_id)
    lake = await _lake_da_fonte(fonte)
    metricas: dict = {}

    async def _corpo() -> dict:
        if not replay:
            return await motor.processar_fatia(
                fonte=fonte,
                execucao_id=execucao_id,
                fatia=fatia_de_json(fatia_ctx),
                lake=lake,
                metricas=metricas,
            )
        contagens: dict = {}
        lotes = await repo.lotes_da_fonte(uuid.UUID(str(fonte_id)), chaves=[fatia_ctx["chave"]])
        for lote in lotes:
            _acumular(
                contagens,
                await motor.reprocessar_fatia(
                    fonte=fonte,
                    execucao_id=execucao_id,
                    lote=lote,
                    lake=lake,
                    metricas=metricas,
                ),
            )
        return contagens

    # `tentativa` is left out: here the Temporal activity does the retrying and it is the one that
    # knows the number (`activity.info().attempt`); this module runs the same on the first and
    # third attempt.
    return await _com_log(
        fonte_id=fonte_id,
        execucao_id=execucao_id,
        fatia_ctx=fatia_ctx,
        corpo=_corpo,
        dataset=fonte["dataset"],
        metricas=metricas,
    )


# Semaphore PER SOURCE and PER PROCESS (not per run): the contract's `concorrencia` is the cap on
# simultaneous requests against the public origin, and two runs of the same source in the same
# worker (retry + incremental, sliced backfill) would add two semaphores, i.e. 2x the cap, which is
# exactly what gets an IP blocked by the origin. Also keyed by event loop: a `Semaphore` belongs to
# the loop it was born in, and tests run one loop per test.
_SEMAFOROS: dict[tuple[int, str, int], asyncio.Semaphore] = {}


def semaforo_da_fonte(fonte_id: uuid.UUID | str, limite: int) -> asyncio.Semaphore:
    chave = (id(asyncio.get_running_loop()), str(fonte_id), limite)
    sem = _SEMAFOROS.get(chave)
    if sem is None:
        sem = asyncio.Semaphore(limite)
        _SEMAFOROS[chave] = sem
    return sem


async def processar(
    *,
    fonte_id: uuid.UUID | str,
    execucao_id: uuid.UUID | str,
    fatias_ctx: list[dict],
    replay: bool = False,
) -> dict:
    """Runs ALL slices and finalizes the run. Inline path (`CFLOW_USAR_TEMPORAL=false`) and the
    basis of what the workflow does slice by slice."""
    fonte = await repo.obter_fonte(fonte_id)
    lake = await _lake_da_fonte(fonte)
    contagens: dict = {}
    t0 = time.monotonic()
    try:
        if replay:
            # Grouped by slice (rather than a single pass over all batches) so the log records one
            # line PER SLICE in replay too; otherwise replay would be the only run mode without a
            # log, precisely the one used when something went wrong.
            chaves = [f["chave"] for f in fatias_ctx]
            por_chave: dict[str, list[dict]] = {}
            for lote in await repo.lotes_da_fonte(uuid.UUID(str(fonte_id)), chaves=chaves):
                por_chave.setdefault(lote["chave_lote"], []).append(lote)

            for d in fatias_ctx:
                metricas: dict = {}
                _acumular(
                    contagens,
                    await _com_log(
                        fonte_id=fonte_id,
                        execucao_id=execucao_id,
                        fatia_ctx=d,
                        corpo=partial(
                            _replay_dos_lotes,
                            fonte,
                            execucao_id,
                            por_chave.get(d["chave"], []),
                            lake,
                            metricas,
                        ),
                        dataset=fonte["dataset"],
                        metricas=metricas,
                        tentativa=1,
                    ),
                )
        else:
            limite_conc = limite(fonte["contrato"], "concorrencia", 4)
            sem = semaforo_da_fonte(fonte_id, max(1, limite_conc))

            async def _uma(d: dict) -> dict:
                # `tentativa=1` because the inline path does not retry: it runs the slice once
                # and raises the failure. Where there is a retry (Temporal) the number is unknown
                # here.
                metricas: dict = {}
                async with sem:
                    return await _com_log(
                        fonte_id=fonte_id,
                        execucao_id=execucao_id,
                        fatia_ctx=d,
                        corpo=lambda: motor.processar_fatia(
                            fonte=fonte,
                            execucao_id=execucao_id,
                            fatia=fatia_de_json(d),
                            lake=lake,
                            metricas=metricas,
                        ),
                        dataset=fonte["dataset"],
                        metricas=metricas,
                        tentativa=1,
                    )

            for r in await asyncio.gather(*(_uma(d) for d in fatias_ctx)):
                _acumular(contagens, r)
    except Exception as exc:  # noqa: BLE001 — INFRA failure closes the run as failed
        resumo = totais(contagens)
        await repo.finalizar_execucao(execucao_id, contagens=resumo, erro=_mensagem(exc))
        _log_execucao_concluida(
            fonte_id=fonte_id,
            dataset=fonte["dataset"],
            execucao_id=execucao_id,
            status="falha",
            resumo=resumo,
            fatias=len(fatias_ctx),
            duracao_ms=int((time.monotonic() - t0) * 1000),
            erro=exc,
        )
        raise
    await concluir(
        fonte_id=fonte_id,
        execucao_id=execucao_id,
        contagens=contagens,
        fatias_ctx=fatias_ctx,
        dataset=fonte["dataset"],
        duracao_ms=int((time.monotonic() - t0) * 1000),
    )
    return totais(contagens)


async def falhar(
    *,
    execucao_id: uuid.UUID | str,
    erro: str,
    contagens: dict,
    fonte_id: uuid.UUID | str | None = None,
    dataset: str | None = None,
    fatias: int = 0,
    duracao_ms: int | None = None,
) -> None:
    """Closes the run as FAILED, in the database **and in the log**. Counterpart of `concluir`.

    It exists because the worker used to call `repo.finalizar_execucao` directly: the database
    recorded the failure and the log said nothing. Since `CFLOW_USAR_TEMPORAL=true` is the default,
    **the failure path was blind exactly on the event one comes to investigate**, as
    `ingestao_execucao_concluida` was emitted only by the inline path, which does not run in
    production.

    Even on failure the counts of what ALREADY landed are recorded: a run that brought 8 of 10
    slices and died on the ninth is not "zero", and the watermark (which only `concluir` advances)
    stays where it is.

    `fonte_id`/`dataset`/`fatias` are optional because the Temporal activity does not receive them;
    it knows the run, not the source. The log is emitted even without them: `execucao_id` ties this
    line to `ingestao_execucao_iniciada`, which already carried source and dataset. A final line
    missing some fields is far better than no final line.
    """
    resumo = totais(contagens or {})
    await repo.finalizar_execucao(execucao_id, contagens=resumo, erro=erro)
    _log_execucao_concluida(
        fonte_id=fonte_id or "?",
        dataset=dataset,
        execucao_id=execucao_id,
        status="falha",
        resumo=resumo,
        fatias=fatias,
        duracao_ms=duracao_ms,
        erro=ErroAplicacao(codigo="erro_infra", mensagem=erro, status=500),
    )


def _mensagem(exc: Exception) -> str:
    return exc.mensagem if isinstance(exc, ErroAplicacao) else "Ingestion failed."


def _log_execucao_concluida(
    *,
    fonte_id: uuid.UUID | str,
    dataset: str | None,
    execucao_id: uuid.UUID | str,
    status: str,
    resumo: dict,
    fatias: int,
    duracao_ms: int | None,
    erro: Exception | None = None,
) -> None:
    """Closes the run in the log with the SIX outcomes that `totais()` computes.

    `origem` and `replay` are NOT repeated here: `preparar` knows them, and they were already
    emitted on the `ingestao_execucao_iniciada` line with the same `execucao_id`. Repeating them
    would require carrying them through three layers to the worker, which finalizes through another
    path, and a wrong value in one of them would be worse than its absence, since the final line is
    the one people read first."""
    campos = {
        "fonte": str(fonte_id),
        "dataset": dataset,
        "execucao_id": str(execucao_id),
        "status": status,
        "fatias": fatias,
        "duracao_ms": duracao_ms,
        **{f"lotes_{k}" if k != "lotes" else k: v for k, v in resumo.items()},
    }
    if erro is not None:
        campos["codigo"] = erro.codigo if isinstance(erro, ErroAplicacao) else "erro_infra"
        # `motivo`, not `mensagem` (see `_com_log`): it would overwrite the event name.
        campos["motivo"] = motor.sanear_texto(_mensagem(erro))[:200]
    logger.log(
        logging.WARNING if status == "falha" else logging.INFO,
        "ingestao_execucao_concluida",
        extra={"extra_campos": campos},
    )


async def _watermark_atual(fonte_id: uuid.UUID | str) -> str | None:
    """The mark BEFORE trying to advance it. Without it, `ingestao_watermark_avancou` would also be
    emitted when the `UPDATE ... WHERE chave_lote < EXCLUDED` moves nothing (replay of an old
    window); an "advanced" event that did not advance is worse than no event."""
    async with transacao() as conn:
        return (
            await conn.execute(
                text("SELECT chave_lote FROM ingestao_watermarks WHERE fonte_id = :f"),
                {"f": uuid.UUID(str(fonte_id))},
            )
        ).scalar_one_or_none()


def _acumular(destino: dict, parcial: dict) -> None:
    for k, v in (parcial or {}).items():
        destino[k] = destino.get(k, 0) + v


def totais(c: dict) -> dict:
    novos, reusados = c.get("novo", 0), c.get("reusado", 0) + c.get("nao_modificado", 0)
    quarentena = c.get("quarentena", 0)
    # `nao_publicados` stays OUT of `lotes`: the slice does not exist at the origin, so there is no batch.
    return {
        "lotes": novos + reusados + quarentena,
        "novos": novos,
        "reusados": reusados,
        "quarentena": quarentena,
        "nao_modificados": c.get("nao_modificado", 0),
        "nao_publicados": c.get("nao_publicado", 0),
    }


async def concluir(
    *,
    fonte_id: uuid.UUID | str,
    execucao_id: uuid.UUID | str,
    contagens: dict,
    fatias_ctx: list[dict],
    dataset: str | None = None,
    duracao_ms: int | None = None,
) -> None:
    """Finalizes the run and ADVANCES the watermark.

    The watermark only advances if no slice went to quarantine: marking "reached August" with July
    quarantined would make the gap never be retried by the incremental run.

    `dataset`/`duracao_ms` only feed the log and are optional on purpose: the worker finalizes
    through this same function without timing the run, and a `0` there would be an invented
    duration."""
    resumo = totais(contagens)
    if fatias_ctx and resumo["quarentena"] == 0:
        para = max(f["chave"] for f in fatias_ctx)
        de = await _watermark_atual(fonte_id)
        await repo.gravar_watermark(
            fonte_id,
            para,
            {"execucao_id": str(execucao_id), **resumo},
        )
        # Same lexicographic comparison as the repository's `WHERE chave_lote < EXCLUDED.chave_lote`
        # (keys are YYYY / YYYY-MM / YYYY-MM-DD, so lexicographic order is chronological).
        if de is None or de < para:
            logger.info(
                "ingestao_watermark_avancou",
                extra={
                    "extra_campos": {
                        "fonte": str(fonte_id),
                        "dataset": dataset,
                        "execucao_id": str(execucao_id),
                        "de": de,
                        "para": para,
                    }
                },
            )
    await repo.finalizar_execucao(execucao_id, contagens=resumo)
    _log_execucao_concluida(
        fonte_id=fonte_id,
        dataset=dataset,
        execucao_id=execucao_id,
        status="concluida",
        resumo=resumo,
        fatias=len(fatias_ctx),
        duracao_ms=duracao_ms,
    )


async def executar(
    *,
    fonte_id: uuid.UUID,
    origem: str = "manual",
    inicio: date | None = None,
    fim: date | None = None,
    replay: bool = False,
    ator_id: uuid.UUID | None,
    ip: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Single trigger point. With Temporal, schedules the workflow on the **ingestion's own queue**
    and returns 'executando'; without Temporal, runs inline and returns the result.

    The separate queue is not pedantry: a ten-year backfill is hundreds of activities, and on the
    shared queue they would push tomorrow's report run back."""
    return await _disparar(
        fonte_id=fonte_id,
        origem=origem,
        inicio=inicio,
        fim=fim,
        replay=replay,
        ator_id=ator_id,
        ip=ip,
        request_id=request_id,
    )


async def _disparar(
    *,
    fonte_id: uuid.UUID,
    origem: str,
    inicio: date | None,
    fim: date | None,
    replay: bool,
    ator_id: uuid.UUID | None,
    ip: str | None,
    request_id: str | None,
    fatias_forcadas: list[dict] | None = None,
) -> dict:
    """Prepares and dispatches. Separate from `executar` so the retry can reuse the dispatch
    without reusing the window cut (it brings the slices ready-made)."""
    ctx = await preparar(
        fonte_id=fonte_id,
        origem="replay" if replay else origem,
        inicio=inicio,
        fim=fim,
        ator_id=ator_id,
        ip=ip,
        request_id=request_id,
        fatias_forcadas=fatias_forcadas,
    )
    cfg = get_settings()
    if not ctx["fatias"] and not replay:
        resumo = totais({})
        await repo.finalizar_execucao(ctx["execucao_id"], contagens=resumo)
        # Close the pair in the log: a run that opened with no slices and no closing line looks
        # like a stuck run to anyone reading only the log.
        _log_execucao_concluida(
            fonte_id=fonte_id,
            dataset=ctx["dataset"],
            execucao_id=ctx["execucao_id"],
            status="concluida",
            resumo=resumo,
            fatias=0,
            duracao_ms=None,
        )
        return {**ctx, "status": "concluida", "totais": resumo}

    if cfg.usar_temporal:
        from temporalio.client import Client

        cliente = await Client.connect(cfg.temporal_host)
        await cliente.start_workflow(
            "IngerirFonte",
            args=[str(fonte_id), ctx["execucao_id"], ctx["fatias"], replay, ctx["concorrencia"]],
            id=f"ingestao-{ctx['execucao_id']}",
            task_queue=cfg.ingestao_task_queue,
        )
        return {**ctx, "status": "executando"}

    resumo = await processar(
        fonte_id=fonte_id, execucao_id=ctx["execucao_id"], fatias_ctx=ctx["fatias"], replay=replay
    )
    return {**ctx, "status": "concluida", "totais": resumo}


# --- Per-slice retry ---------------------------------------------------------------------


async def retentar(
    *,
    execucao_id: uuid.UUID,
    ator_id: uuid.UUID | None,
    ip: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Redoes **only the slices that ended badly** in a run (like clearing a task in a scheduler).

    Creates a NEW run (origin `retentativa`) instead of reopening the previous one: a run is a
    record of what happened in a time window, and rewriting yesterday's outcome would erase the
    evidence that a failure happened. The audit trail does not allow deletion; the ingestion
    history follows the same discipline.

    The slice set comes from `repo.fatias_retentaveis`: the caller does not choose the window, only
    among what the platform recorded as unsuccessful. That keeps the network cost bounded by the
    original run's and is what lets retry be a specialist action while backfill is an admin one."""
    async with transacao() as conn:
        anterior = await repo.consultar_execucao(conn, execucao_id)
        pendentes = await repo.fatias_retentaveis(conn, execucao_id)
    if not pendentes:
        raise ErroAplicacao(
            codigo="nada_a_retentar",
            mensagem="No slice of this run ended in failure or quarantine.",
            status=409,
        )
    forcadas = [
        {
            "chave": p["chave_lote"],
            # An old slice may have no recorded window (the slice log came later): without it, the
            # day of the key itself is the honest guess, and it is what `piso_data` already reads.
            "inicio": (p["janela_inicio"] or _inicio_da_chave(p["chave_lote"])).isoformat(),
            "fim": (
                p["janela_fim"] or p["janela_inicio"] or _inicio_da_chave(p["chave_lote"])
            ).isoformat(),
        }
        for p in pendentes
        if p["janela_inicio"] or _inicio_da_chave(p["chave_lote"])
    ]
    if not forcadas:
        raise ErroAplicacao(
            codigo="fatia_sem_janela",
            mensagem="The failed slices have no recorded window; use backfill with the desired window.",
            status=409,
        )
    return await _disparar(
        fonte_id=anterior["fonte_id"],
        origem="retentativa",
        inicio=None,
        fim=None,
        replay=False,
        ator_id=ator_id,
        ip=ip,
        request_id=request_id,
        fatias_forcadas=forcadas,
    )


# --- Ingestion as a PIPELINE NODE ---------------------------------------------------------------


async def executar_no_pipeline(
    *,
    fonte_id: uuid.UUID | str,
    inicio: date | None = None,
    fim: date | None = None,
    ator_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> dict:
    """Ingestion running as a node of a Studio graph. Returns the PER-SLICE LOG, which becomes the
    node's data.

    **Always inline, never dispatching to the `cflow-ingestao` queue.** The node is already inside
    an orchestrator activity; opening another workflow and waiting for it would create two retry
    units for the same work, and the outer one would not know what the inner one already did. The
    reason for the dedicated queue was fan-out: a ten-year backfill becomes hundreds of activities
    and would push tomorrow's report back. As a node, the whole source is **one** activity, and
    concurrency across slices is still bounded by the contract's semaphore, so the queue argument
    does not apply to this path. Backfill and replay go through the API, on the dedicated queue, as
    before.

    The output is the log, not the ingested data: the data is in the lake (bronze), and the lake
    source node reads it. A node returning millions of rows as a snapshot would duplicate the
    dataset inside the run's Postgres/Parquet for nobody to read."""
    ctx = await preparar(
        fonte_id=uuid.UUID(str(fonte_id)),
        origem="pipeline",
        inicio=inicio,
        fim=fim,
        ator_id=ator_id,
        request_id=request_id,
    )
    if not ctx["fatias"]:
        resumo = totais({})
        await repo.finalizar_execucao(ctx["execucao_id"], contagens=resumo)
        _log_execucao_concluida(
            fonte_id=fonte_id,
            dataset=ctx["dataset"],
            execucao_id=ctx["execucao_id"],
            status="concluida",
            resumo=resumo,
            fatias=0,
            duracao_ms=None,
        )
    else:
        # `processar` already returns `totais(...)`: six integers. Re-reading the run to rebuild
        # the same numbers would be an extra query AND a trap: `consultar_execucao` returns
        # `{**row, "lotes": [...]}`, so the LIST of batches overwrites the COUNT of the same name,
        # and that list carries `date`/`datetime` values the audit trail JSON cannot serialize.
        resumo = await processar(
            fonte_id=fonte_id,
            execucao_id=ctx["execucao_id"],
            fatias_ctx=ctx["fatias"],
            replay=False,
        )
    async with transacao() as conn:
        log = await repo.listar_fatias(conn, uuid.UUID(ctx["execucao_id"]))
    return {
        "execucao_id": ctx["execucao_id"],
        "dataset": ctx["dataset"],
        "janela": ctx["janela"],
        # `processar` raises on INFRA failure (and the engine turns that into a node with an
        # error); reaching here means the run closed. DATA failure shows up as a quarantined slice
        # in the log.
        "status": "concluida",
        "totais": resumo,
        "fatias": log,
    }


# --- Cadence scheduling ----------------------------------------------------------------


async def fontes_devidas() -> list[dict]:
    """Active sources whose `proxima_em` is due. Sets the next window BEFORE triggering (same
    transaction): two ticks of the loop must not trigger the same source twice."""
    agora = agora_utc()
    devidas: list[dict] = []
    async with transacao() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT id, nome, cadencia FROM ingestao_fontes "
                        "WHERE ativo = true AND cadencia <> 'manual' AND proxima_em IS NOT NULL "
                        "AND proxima_em <= :agora FOR UPDATE SKIP LOCKED"
                    ),
                    {"agora": agora},
                )
            )
            .mappings()
            .all()
        )
        for r in rows:
            proxima = agora + INTERVALO.get(r["cadencia"], timedelta(days=1))
            await conn.execute(
                text("UPDATE ingestao_fontes SET proxima_em = :p WHERE id = :id"),
                {"p": proxima, "id": r["id"]},
            )
            devidas.append({"id": r["id"], "nome": r["nome"]})
    return devidas
