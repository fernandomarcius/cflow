"""Orphan execution reconciliation: close rows shown as `executando` whose workflow is already dead.

An execution status only leaves `executando` when the workflow itself writes the outcome
(`engine.finalizar`). If the workflow dies before getting there (the orchestrator was restarted,
the workflow was terminated, the history expired through namespace retention), the row stays
`executando` **forever** and shows up as "running now" for anyone opening the list.

This is independent of Temporal persistence: even with perfect persistence a workflow can end
without writing its outcome, and the row needs someone to close it.

**The guard that defines this routine:** a failure is only recorded when Temporal **answered** and
said it does not know the workflow. With Temporal down EVERY execution looks orphaned: a connection
failure is not an answer, and in that case nothing is touched. Same lesson as
`expurgar_snapshots_orfaos`: a routine that decides by deduction must tell "the other side said no"
apart from "the other side said nothing".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text

from ..audit import Acao, AtorTipo, registrar
from ..config import get_settings
from ..db import transacao

# Workflow id formats in use: a standalone execution and an execution that is part of a pipeline
# DAG. An execution uses one OR the other, so the lookup tries both before concluding that nobody
# knows it.
PREFIXOS_WORKFLOW = ("exec-", "dag-")

MENSAGEM_ORFA = (
    "Execution interrupted: the orchestrator no longer knows this workflow "
    "(likely a service restart). No result was produced."
)


@dataclass
class Veredito:
    """What Temporal answered about an execution. `desconhecida` is only True if it ANSWERED."""

    viva: bool
    desconhecida: bool
    detalhe: str | None = None


async def consultar_uma(
    cliente,
    execucao_id: uuid.UUID,
    prefixos: tuple[str, ...] = PREFIXOS_WORKFLOW,
    *,
    raiz: uuid.UUID | None = None,
) -> Veredito:
    """Ask Temporal for the state of this execution's workflow.

    Transport exceptions propagate to the caller: a network error must NOT become "unknown",
    otherwise an unstable Temporal closes executions that are running.

    Public because the duplicate-execution guard (`engine.criar_execucao`) asks the SAME question
    inline: a `executando` row whose workflow died must not block the next trigger. `prefixos`
    exists for ingestion, whose workflow is `ingestao-{id}`.

    **`raiz`: a chain member has no workflow with ITS id.** The whole chain runs in an
    `ExecutarDAG` named `dag-<root execution>` (`disparo.disparar`); `exec-<member>` and
    `dag-<member>` never existed. Without asking for the root's workflow, every member older than
    `idade_minima_minutos` since pre-creation was declared orphaned while the DAG was alive:
    measured with 5 simultaneous chains, 4 of 5 failed this way, and the "orphan" member still
    completed afterwards, on top of the failure.
    """
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCError, RPCStatusCode

    ids = [f"{prefixo}{execucao_id}" for prefixo in prefixos]
    if raiz is not None and raiz != execucao_id:
        ids.append(f"dag-{raiz}")
    nao_encontrados = 0
    for wid in ids:
        try:
            desc = await cliente.get_workflow_handle(wid).describe()
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                nao_encontrados += 1
                continue
            raise  # any other code is a transport/server problem: conclude nothing
        if desc.status == WorkflowExecutionStatus.RUNNING:
            return Veredito(viva=True, desconhecida=False)
        # It ended somehow and the row is still `executando`, so `finalizar` did not run.
        # Whatever the outcome in Temporal, there is no result here: close as failure.
        return Veredito(
            viva=False,
            desconhecida=False,
            detalhe=f"workflow {wid} ended as {desc.status.name}",
        )
    if nao_encontrados == len(ids):
        return Veredito(viva=False, desconhecida=True)
    return Veredito(viva=True, desconhecida=False)  # unreachable in practice; conservative


async def conectar(host: str):
    """Temporal client for QUERYING. A function of its own (instead of `Client.connect` in the
    callers) so a test can replace only the liveness query without breaking the real
    `start_workflow` of the route under test: both live in `temporalio.client.Client`."""
    from temporalio.client import Client

    return await Client.connect(host)


async def fechar_orfa(execucao_id: uuid.UUID, motivo: str) -> None:
    """Mark the execution (and its pending nodes) as failed, and audit it.

    The nodes go along because `execucao_nos.status` has the same default `executando`: closing
    only the execution would leave the detail view showing "running" nodes inside a failed run.
    """
    async with transacao() as conn:
        antes = (
            await conn.execute(
                text("SELECT status::text, iniciada_em FROM execucoes WHERE id = :id"),
                {"id": execucao_id},
            )
        ).first()
        if antes is None or antes[0] != "executando":
            return  # finished between the query and now: whoever got there first is right
        await conn.execute(
            text(
                "UPDATE execucoes SET status='falha', erro=:e, concluida_em=now() "
                "WHERE id = :id AND status='executando'"
            ),
            {"id": execucao_id, "e": motivo},
        )
        await conn.execute(
            text(
                "UPDATE execucao_nos SET status='falha', erro=:e, concluido_em=now() "
                "WHERE execucao_id = :id AND status='executando'"
            ),
            {"id": execucao_id, "e": motivo},
        )
        await registrar(
            conn,
            acao=Acao.EDITAR,
            recurso_tipo="execucao",
            recurso_id=execucao_id,
            ator_tipo=AtorTipo.SISTEMA,
            antes={"status": "executando"},
            depois={"status": "falha", "erro": motivo},
        )


async def fechar_nos_pendentes(*, atraso_minutos: int = 5, simular: bool = False) -> int:
    """Close `executando` nodes whose EXECUTION has already ended. Asks Temporal nothing.

    Found in the field: an execution in `falha` with a node still `executando`. Workflow-based
    reconciliation does not reach it: it scans `executando` executions, and this one already had
    an outcome. It is the same lie on screen one level down: the detail shows a "running" node
    inside something that is over.

    No orchestrator guard fits here: the execution itself says it ended, and the database is the
    source of truth for that claim. The guard needed is against a **race**: `atraso_minutos`
    avoids competing with a `finalizar` that is still writing the last nodes.
    """
    sql_alvo = (
        "SELECT n.id FROM execucao_nos n JOIN execucoes e ON e.id = n.execucao_id "
        "WHERE n.status = 'executando' AND e.status <> 'executando' "
        "AND e.concluida_em < now() - make_interval(mins => :m)"
    )
    async with transacao() as conn:
        alvos = [r[0] for r in (await conn.execute(text(sql_alvo), {"m": atraso_minutos})).all()]
        if not alvos or simular:
            return len(alvos)
        await conn.execute(
            text(
                "UPDATE execucao_nos SET status='falha', erro=:e, concluido_em=now() "
                "WHERE id = ANY(:ids)"
            ),
            {
                "e": "Node without outcome: the execution ended without recording this node's result.",
                "ids": alvos,
            },
        )
    return len(alvos)


async def reconciliar_execucoes_orfas(
    *, idade_minima_minutos: int = 30, simular: bool = False
) -> dict:
    """Close `executando` executions whose workflow Temporal no longer knows.

    `idade_minima_minutos` covers the race between writing the row in `execucoes` and calling
    `start_workflow`: for a few moments the execution exists in the database and not yet in
    Temporal. Thirty minutes is orders of magnitude above that window and still well below the
    time that bothers someone waiting for the report.

    An execution Temporal says is RUNNING is not touched, however old: slow pipelines are normal,
    and age alone is never a reason to declare something dead.

    `simular=True` asks Temporal ALL the questions and writes nothing, returning the per-execution
    verdict in `detalhes`. It is the same decision as the real mode: a dry run that used another
    criterion could not authorize the real one.
    """
    cfg = get_settings()
    if not cfg.usar_temporal:
        # Inline execution (CFLOW_USAR_TEMPORAL=false): there is no workflow to query, hence no
        # possible answer, and without an answer nothing is marked.
        return {
            "fechadas": 0,
            "vivas": 0,
            "recusado": "inline execution: no orchestrator to query",
        }

    async with transacao() as conn:
        # The chain root comes along: it is the id of the workflow that runs the member (`consultar_uma`).
        candidatas = [
            (r[0], r[1])
            for r in (
                await conn.execute(
                    text(
                        "SELECT e.id, (SELECT r.id FROM execucoes r "
                        "              WHERE e.cadeia_id IS NOT NULL AND r.cadeia_id = e.cadeia_id "
                        "                AND r.cadeia_raiz AND r.id <> e.id LIMIT 1) AS raiz "
                        "FROM execucoes e WHERE e.status = 'executando' "
                        "AND e.iniciada_em < now() - make_interval(mins => :m) "
                        "ORDER BY e.iniciada_em"
                    ),
                    {"m": idade_minima_minutos},
                )
            ).all()
        ]
    if not candidatas:
        return {"fechadas": 0, "vivas": 0}

    try:
        cliente = await conectar(cfg.temporal_host)
    except Exception as e:  # noqa: BLE001 - orchestrator down is no verdict on anything
        return {
            "fechadas": 0,
            "vivas": 0,
            "candidatas": len(candidatas),
            "recusado": f"orchestrator unreachable ({type(e).__name__}); no execution touched",
        }

    fechadas, vivas, inconclusivas = 0, 0, 0
    detalhes: list[dict] = []
    for eid, raiz in candidatas:
        try:
            v = await consultar_uma(cliente, eid, raiz=raiz)
        except Exception as e:  # noqa: BLE001 - the query for THIS one failed: skip, conclude nothing
            inconclusivas += 1
            detalhes.append(
                {"execucao_id": str(eid), "veredito": f"inconclusive ({type(e).__name__})"}
            )
            continue
        if v.viva:
            vivas += 1
            detalhes.append({"execucao_id": str(eid), "veredito": "running"})
            continue
        motivo = v.detalhe or MENSAGEM_ORFA
        if not simular:
            await fechar_orfa(eid, motivo)
        fechadas += 1
        detalhes.append({"execucao_id": str(eid), "veredito": "orphan", "motivo": motivo})

    resultado = {
        "fechadas": fechadas,
        "vivas": vivas,
        "candidatas": len(candidatas),
        "detalhes": detalhes,
        "simulado": simular,
    }
    if inconclusivas:
        resultado["inconclusivas"] = inconclusivas
    return resultado


CHAVE_TRAVA_RECONCILIACAO = "cflow:reconciliacao"


async def reconciliar_com_trava(*, idade_minima_minutos: int = 30) -> dict:
    """`reconciliar_execucoes_orfas` under a Postgres advisory lock, to run in a loop in the worker.

    The worker may have N replicas, each with the loop. Without a lock, N replicas run the same
    round of `DescribeWorkflow` against Temporal at the same instant: correct (closing is
    idempotent: `fechar_orfa` checks `status='executando'` before writing) but useless load on the
    orchestrator exactly when it may be recovering.

    `pg_try_advisory_xact_lock` does not block: whoever did not get it returns
    `{"pulado": True}` and tries again on the next tick. The lock belongs to the transaction, so
    it releases on leaving the `with`, even on exception; the inner queries open their own
    connections (`transacao()`), so no deadlock.
    """
    async with transacao() as conn:
        pegou = (
            await conn.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:chave))"),
                {"chave": CHAVE_TRAVA_RECONCILIACAO},
            )
        ).scalar_one()
        if not pegou:
            return {"pulado": True, "fechadas": 0, "vivas": 0}
        return await reconciliar_execucoes_orfas(idade_minima_minutos=idade_minima_minutos)
