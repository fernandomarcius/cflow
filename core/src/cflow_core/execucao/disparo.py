"""Dispatch of SCHEDULED runs: chained execution (DAG of pipelines).

A chained run executes every pipeline of the chain in topological order in a single pass, and EVERY
consumer reads the EXACT snapshot produced IN THAT pass (the freshness map
`{pipeline_id -> execucao_id}`). Without it, a `pipeline`-type source falls back to "latest finished
run, whichever it was" (`snapshots.resolver_snapshot`) and the result is internally inconsistent
(plausible but wrong), or fails with `sem_execucao` if the upstream never ran.

This module exists so the HTTP route and the WORKER share the SAME dispatch. The worker depends only
on `cflow-core` + `temporalio` (it cannot import `cflow_api`); writing the chain pre-creation twice
would give schedules and the "Run now" button different semantics.

Temporal stays INJECTED (same discipline as the scheduler's `Dispatch`): `cflow_core` does not import
`temporalio`. Whoever owns the client passes `iniciar_workflow(nome, args, workflow_id)`; without it
the dispatch runs INLINE (the `CFLOW_USAR_TEMPORAL=false` path, also used by tests).

DEGENERATION IS A REQUIREMENT: a chain with ONE pipeline behaves exactly as before the DAG: same
call, same workflow (`ExecutarPipeline`), null `cadeia_id`, and an undeclared parameter is still an
error.

THREE GUARDS live here rather than in the schedule CRUD, because the CHAIN changes after the
schedule is created (a new upstream version is published, or a pipeline-type source is added) and
the schedule never goes through a CRUD again; dispatch is the only point evaluated when the values
would be used: refusal of a `segredo: true` parameter stored in clear text
(`_recusar_segredo_guardado`), refusal of a parameter that NO pipeline in the chain declares
(`definicao.validar_parametros_da_cadeia`), and finalisation of pre-created runs that will not run
(`abortar`).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import text

from ..db import transacao
from ..errors import DetalheErro, ErroAplicacao
from ..pipelines.dag import resolver_ordem
from ..pipelines.definicao import (
    filtrar_parametros_declarados,
    montante_publica,
    nomes_segredo,
    validar_parametros_da_cadeia,
)
from . import engine
from .reexecucao import TIPOS_EFEITO, publicacao_prevista

# `iniciar_workflow(nome, args, workflow_id)` is supplied by whoever owns the Temporal client.
Iniciador = Callable[[str, list, str], Awaitable[None]]


async def abortar(execucoes: list[str], motivo: str) -> None:
    """Finalise as FAILED runs that were already created but will not run. Best-effort, one by one.

    Pre-creation and `start_workflow` are not atomic: between creating the chain's first run and
    Temporal accepting the workflow, anything (database blip, full queue, dropped client) leaves a
    run in 'executando' FOREVER, and the `ExecutarDAG` try/except cannot reach it because the
    workflow never started. A failure while finalising must not hide the ORIGINAL failure, so each
    one is swallowed here."""
    for eid in execucoes:
        try:
            await engine.finalizar(
                execucao_id=uuid.UUID(eid), erro=motivo, somente_se_executando=True
            )
        except Exception:  # noqa: BLE001
            # Swallowing silently would leave the run stuck in 'executando' with no trace. Log with
            # the traceback, and never re-raise: the ORIGINAL failure must reach the caller.
            logging.getLogger("cflow_core.disparo").exception(
                "abortar_execucao_falhou", extra={"extra_campos": {"execucao_id": eid}}
            )
            continue


def _recusar_segredo_guardado(ordem: list[dict], parametros: dict | None) -> None:
    """Refuse to DISPATCH a schedule whose stored parameters include a `segredo: true` declared by
    ANY pipeline of the chain.

    The CRUD refusal (`agendador._recusar_segredo_no_agendamento`) is evaluated once, on
    create/edit; the CHAIN changes LATER (an upstream publishes a version that declares `senha_api`
    as a credential, or a pipeline-type source is added). The schedule never goes through a CRUD
    again, and the clear-text value already in `agendamentos.parametros` (metadata, not a vault)
    would be DELIVERED to the pipeline that now declares it. The refusal must also hold here, the
    only point evaluated when the value would be used."""
    if not parametros:
        return
    segredos = set().union(*(nomes_segredo(i["definicao"]) for i in ordem)) & set(parametros)
    if segredos:
        raise ErroAplicacao(
            codigo="agendamento_com_segredo",
            mensagem=(
                "The schedule sets a credential parameter "
                f"({', '.join(sorted(segredos))}) declared in the chain: the value is stored in "
                "clear text in the schedule and cannot be used. Store the credential in the "
                "connection vault and recreate the schedule."
            ),
            status=400,
        )


def _efeitos_a_montante(ordem: list[dict], raiz: str) -> list[tuple[str, str, str]]:
    """`(pipeline_id, no_id, tipo)` of every external-effect node OUTSIDE the root."""
    return [
        (item["pipeline_id"], no.get("id"), no.get("tipo"))
        for item in ordem
        if item["pipeline_id"] != raiz
        for no in (item["definicao"].get("nos") or [])
        if no.get("tipo") in TIPOS_EFEITO
    ]


def decidir_montante(
    ordem: list[dict], raiz_id: uuid.UUID | str, *, nomes: dict[str, str] | None = None
) -> bool:
    """Does the upstream of this chain run WITHOUT PUBLISHING? Decided by the ROOT definition
    (`montante_publica: false`); the flag on an upstream is ignored, since it describes the
    requested run, not the pipelines that ran because of table dependencies.

    With the flag set, an upstream EFFECT node (`escrita_sql`, `http`, `email`, `ingestao`) is a
    loud refusal naming pipeline and node: skipping the effect would make its snapshot (the log of
    what was sent) lie, and executing it in a run that claims to have no effect would send the
    e-mail and write to the external database on every dispatch. The only honest answer is to
    refuse. `nomes` ({pipeline_id -> name}) is only used for the message."""
    raiz = str(raiz_id)
    raiz_def = next((i["definicao"] for i in ordem if i["pipeline_id"] == raiz), None)
    if raiz_def is None or montante_publica(raiz_def):
        return False
    efeitos = _efeitos_a_montante(ordem, raiz)
    if efeitos:
        nomes = nomes or {}
        rotulos = [
            f"'{nomes.get(pid, pid)}' -> node '{no_id}' ({tipo})" for pid, no_id, tipo in efeitos
        ]
        raise ErroAplicacao(
            codigo="montante_com_efeito",
            mensagem=(
                'The root declares "montante_publica": false, but the upstream has an external '
                f"effect: {'; '.join(rotulos)}. A run that does not publish cannot write outside "
                "the platform: remove the effect from the upstream or the flag from the root."
            ),
            status=400,
            detalhes=[
                DetalheErro(
                    campo="montante_publica",
                    mensagem=f"Pipeline '{nomes.get(pid, pid)}' ({pid}), node '{no_id}' ({tipo}).",
                )
                for pid, no_id, tipo in efeitos
            ],
        )
    return True


async def planejar_montante(ordem: list[dict], raiz_id: uuid.UUID | str) -> bool:
    """`decidir_montante` with the pipeline NAMES in the refusal: the id alone does not tell the
    reader which pipeline has the effect. Only queries the database when there is something to
    refuse.

    Called BEFORE any run exists (pre-creation or inline): refusing later would leave the
    pre-created chain in 'executando' waiting for an `abortar`."""
    raiz = str(raiz_id)
    raiz_def = next((i["definicao"] for i in ordem if i["pipeline_id"] == raiz), None)
    nomes: dict[str, str] = {}
    if raiz_def is not None and not montante_publica(raiz_def):
        pids = sorted({pid for pid, _, _ in _efeitos_a_montante(ordem, raiz)})
        if pids:
            async with transacao() as conn:
                rows = await conn.execute(
                    text("SELECT id::text, nome FROM pipelines WHERE id::text = ANY(:ids)"),
                    {"ids": pids},
                )
                nomes = {r[0]: r[1] for r in rows}
    return decidir_montante(ordem, raiz, nomes=nomes)


def resumir_cadeia(ordem: list[dict], raiz_id: uuid.UUID | str, nomes: dict[str, str]) -> dict:
    """What a dispatch of `raiz_id` runs TOGETHER with it, without dispatching anything.

    This is what the UI shows BEFORE scheduling or "run now": scheduling a consumer runs its
    upstreams with all their effects, which must not be discovered afterwards in the audit trail.
    Each upstream comes with its (named) external effects and the outputs it publishes;
    `sem_publicar` says whether the root disables upstream publication; `recusa` carries the refusal
    the dispatch would give (upstream effect under `montante_publica: false`) as data, not as an
    error, since nothing was dispatched. The decision is the SAME function as dispatch
    (`decidir_montante`): the UI never shows a different rule."""
    raiz = str(raiz_id)
    try:
        sem_publicar = decidir_montante(ordem, raiz, nomes=nomes)
        recusa = None
    except ErroAplicacao as e:
        sem_publicar, recusa = False, {"codigo": e.codigo, "mensagem": e.mensagem}
    montantes = []
    for item in ordem:
        pid = item["pipeline_id"]
        if pid == raiz:
            continue
        nos = item["definicao"].get("nos") or []
        montantes.append(
            {
                "pipeline_id": pid,
                "nome": nomes.get(pid, pid),
                "versao": item["versao_numero"],
                "nivel": item["nivel"],
                "efeitos": [
                    {"no_id": n.get("id"), "tipo": n.get("tipo")}
                    for n in nos
                    if n.get("tipo") in TIPOS_EFEITO
                ],
                "publicacoes": [
                    publicacao_prevista(n.get("id"), n, publica=not sem_publicar)
                    for n in nos
                    if n.get("tipo") == "saida"
                ],
            }
        )
    da_raiz = next((i for i in ordem if i["pipeline_id"] == raiz), None)
    return {
        "pipeline_id": raiz,
        "nome": nomes.get(raiz, raiz),
        "versao": da_raiz["versao_numero"] if da_raiz else None,
        "montantes": montantes,
        "sem_publicar": sem_publicar,
        "recusa": recusa,
    }


async def plano_cadeia(conn, pipeline_id: uuid.UUID | str) -> dict:
    """`resumir_cadeia` for the chain a dispatch of this pipeline would resolve NOW: the same
    `resolver_ordem` as dispatch (upstream at its published version). An upstream without a
    published version or a cycle raises the usual errors (`sem_definicao`, `ciclo_pipelines`): a
    schedule created over such a chain would fail on every dispatch."""
    ordem = await resolver_ordem(conn, pipeline_id, None)
    ids = [item["pipeline_id"] for item in ordem]
    linhas = await conn.execute(
        text("SELECT id::text, nome FROM pipelines WHERE id::text = ANY(:ids)"), {"ids": ids}
    )
    return resumir_cadeia(ordem, pipeline_id, {r[0]: r[1] for r in linhas})


async def preparar_cadeia(
    *,
    ordem: list[dict],
    raiz_id: uuid.UUID | str,
    ator_id: uuid.UUID,
    parametros: dict | None = None,
    pagina_id: uuid.UUID | None = None,
    origem: str = "manual",
    agendamento_id: uuid.UUID | None = None,
    ip=None,
    request_id=None,
) -> dict:
    """PRE-CREATE one run per pipeline in `ordem` (resolved by `dag.resolver_ordem`) and return
    `{raiz, cadeia, fontes_execucao, cadeia_id}`, the payload of the `ExecutarDAG` workflow.

    The freshness map is COMPLETE before any node runs: that is how the root reads the upstream
    snapshot of THIS run rather than "the latest there is".

    Each pipeline receives only the parameters it DECLARES (`filtrar_parametros_declarados`): an
    upstream legitimately declares a subset, and without the slicing pre-creation would die with
    `parametros_invalidos`. `origem` marks ALL runs (the dispatch was one); `agendamento_id` marks
    only the ROOT, the run the schedule asked for, which `ultima_execucao_id` and the UI history
    point to.
    """
    raiz = str(raiz_id)
    cadeia_id = uuid.uuid4()  # same dispatch: enables `fonte` reuse across sibling pipelines
    fontes_execucao: dict[str, str] = {}
    cadeia: list[dict] = []
    # A name that NO pipeline in the chain declares is a loud error, as in the solo case: sliced N
    # times it would vanish silently and the run would go without the slice the name asked for.
    validar_parametros_da_cadeia([i["definicao"] for i in ordem], parametros)
    # Decided BEFORE creating any run: refusing an upstream effect must not leave any pre-created
    # row in 'executando'.
    sem_publicar = await planejar_montante(ordem, raiz)
    criadas: dict[str, dict] = {}
    # The ROOT is created FIRST: only it carries a concurrency key, so a 409 happens BEFORE any
    # dependency is pre-created (which would have to be aborted). Creation order does not matter to
    # `ExecutarDAG`, which decides by each item's `deps`.
    for item in sorted(ordem, key=lambda it: it["pipeline_id"] != raiz):
        pid = item["pipeline_id"]
        try:
            ctx = await engine.criar_execucao(
                pipeline_id=uuid.UUID(pid),
                ator_id=ator_id,
                parametros=filtrar_parametros_declarados(item["definicao"], parametros),
                pagina_id=pagina_id if pid == raiz else None,
                versao_numero=item["versao_numero"],
                origem=origem,
                agendamento_id=agendamento_id if pid == raiz else None,
                cadeia_id=cadeia_id,
                # The root is the REQUESTED run; the others ran because of table dependencies. It is
                # the group header in listings and the only one with the duplicate guard: blocking
                # an upstream would abort the whole chain, and two chains recomputing the same
                # upstream is routine.
                cadeia_raiz=(pid == raiz),
                ip=ip,
                request_id=request_id,
            )
        except Exception:
            # The chain will not run: what was already created must not stay 'executando' forever.
            await abortar(
                list(fontes_execucao.values()),
                "The chain was never dispatched: failed to prepare the runs.",
            )
            raise
        fontes_execucao[pid] = ctx["execucao_id"]
        criadas[pid] = {
            "execucao_id": ctx["execucao_id"],
            "definicao": ctx["definicao"],
            "ordem": ctx["ordem"],
            "parametros": ctx["parametros"],
            "pipeline_id": pid,
            "nivel": item["nivel"],  # ExecutarDAG groups by level
            # Who ExecutarDAG waits for before starting this pipeline (`execucao.paralelo`).
            "deps": item.get("deps", []),
        }
        if sem_publicar and pid != raiz:
            # EXPLICIT per-item signal, never a mutated definition: the definition is the published,
            # audited version, and whoever reads the snapshot later must see what ran. The key only
            # exists when set, so without the flag the `ExecutarDAG` payload is unchanged.
            criadas[pid]["sem_publicar"] = True
    # The response follows TOPOLOGICAL ORDER (dependency first, root last), the contract of
    # `executar-encadeado`; creating the root first is an implementation detail of the guard.
    cadeia = [criadas[item["pipeline_id"]] for item in ordem]
    return {
        "raiz": fontes_execucao[raiz],
        "cadeia": cadeia,
        "fontes_execucao": fontes_execucao,
        "cadeia_id": str(cadeia_id),
    }


async def disparar(
    *,
    pipeline_id: uuid.UUID,
    ator_id: uuid.UUID,
    parametros: dict | None = None,
    pagina_id: uuid.UUID | None = None,
    origem: str = "agendado",
    agendamento_id: uuid.UUID | None = None,
    iniciar_workflow: Iniciador | None = None,
    ip=None,
    request_id=None,
) -> str:
    """Dispatch the pipeline WITH ITS DEPENDENCIES and return the ROOT `execucao_id`.

    A failure in one pipeline aborts the WHOLE CHAIN: running the downstream over a failed upstream
    would produce a plausible but wrong result, which is worse than no result. Inline,
    `executar_encadeado` raises and later levels never exist, so the scheduler counts the dispatch
    as an error and does NOT store `ultima_execucao_id`. Via Temporal the failure is ASYNCHRONOUS:
    `start_workflow` returns immediately and the schedule is left `disparado` pointing at a run that
    will still fail; the run screen shows the outcome, and `ExecutarDAG` marks pre-created runs that
    never ran as failed (otherwise they would sit in 'executando' forever). `erro` on the schedule,
    via Temporal, only means a failure to DISPATCH.
    """
    async with transacao() as conn:
        ordem = await resolver_ordem(conn, pipeline_id, None)

    if agendamento_id is not None:
        # Schedule parameters come from `agendamentos.parametros`, stored in CLEAR text. If the chain
        # started declaring one as a credential after the schedule was created, dispatch is the only
        # point that can still stop the value from reaching the pipeline.
        _recusar_segredo_guardado(ordem, parametros)

    if len(ordem) == 1:
        # DEGENERATION: behaviour IDENTICAL to pre-DAG. `versao_numero` stays None (`criar_execucao`
        # resolves the LATEST version), `cadeia_id` stays null (a solo run is not a chain) and the
        # parameters go through WHOLE: with no other pipeline a parameter could belong to, an
        # undeclared name is a typo and must remain a loud error, never a silent slice.
        if iniciar_workflow is None:
            res = await engine.executar(
                pipeline_id=pipeline_id,
                ator_id=ator_id,
                parametros=parametros,
                pagina_id=pagina_id,
                origem=origem,
                agendamento_id=agendamento_id,
                ip=ip,
                request_id=request_id,
            )
            return res["id"]
        ctx = await engine.criar_execucao(
            pipeline_id=pipeline_id,
            ator_id=ator_id,
            parametros=parametros,
            pagina_id=pagina_id,
            origem=origem,
            agendamento_id=agendamento_id,
            ip=ip,
            request_id=request_id,
        )
        try:
            await iniciar_workflow(
                "ExecutarPipeline",
                [ctx["execucao_id"], ctx["definicao"], ctx["ordem"], ctx["parametros"]],
                f"exec-{ctx['execucao_id']}",
            )
        except Exception:
            # Run created but workflow NOT accepted: without this it stays 'executando' forever.
            await abortar([ctx["execucao_id"]], "Could not start the run.")
            raise
        return ctx["execucao_id"]

    if iniciar_workflow is None:
        res = await engine.executar_encadeado(
            pipeline_id=pipeline_id,
            ator_id=ator_id,
            parametros=parametros,
            pagina_id=pagina_id,
            origem=origem,
            agendamento_id=agendamento_id,
            # The order ALREADY resolved above is passed on: resolving it again would open a window
            # for an upstream to be published BETWEEN the two reads, and the chain that runs would no
            # longer be the one that passed the credential refusal and the parameter check.
            ordem=ordem,
            ip=ip,
            request_id=request_id,
        )
        return res["id"]

    pronto = await preparar_cadeia(
        ordem=ordem,
        raiz_id=pipeline_id,
        ator_id=ator_id,
        parametros=parametros,
        pagina_id=pagina_id,
        origem=origem,
        agendamento_id=agendamento_id,
        ip=ip,
        request_id=request_id,
    )
    try:
        await iniciar_workflow(
            "ExecutarDAG",
            [pronto["cadeia"], pronto["fontes_execucao"], parametros],
            f"dag-{pronto['raiz']}",
        )
    except Exception:
        # The WHOLE chain is already pre-created (that is the freshness map) and no node will run.
        await abortar(
            list(pronto["fontes_execucao"].values()),
            "Could not start the execution chain.",
        )
        raise
    return pronto["raiz"]


def montar_dispatch(
    iniciar_workflow: Iniciador | None = None,
    *,
    ator_id: uuid.UUID | None = None,
    ip=None,
    request_id=None,
):
    """Adapt `disparar` to the scheduler's `Dispatch` signature
    (`pipeline_id, criado_por, parametros, pagina_id, agendamento_id`).

    `ator_id` overrides the schedule creator: the "Run now" route uses it to stamp whoever clicked;
    the worker loop runs without a human actor and uses the schedule's `criado_por`.
    """

    async def _dispatch(pipeline_id, criado_por, parametros, pagina_id, agendamento_id) -> str:
        return await disparar(
            pipeline_id=pipeline_id,
            ator_id=ator_id or criado_por,
            parametros=parametros,
            pagina_id=pagina_id,
            origem="agendado",
            agendamento_id=agendamento_id,
            iniciar_workflow=iniciar_workflow,
            ip=ip,
            request_id=request_id,
        )

    return _dispatch
