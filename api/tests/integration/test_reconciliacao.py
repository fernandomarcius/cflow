"""Orphan execution reconciliation: closes what died, and ONLY what Temporal said died.

The real-world case: executions stuck in `executando`, some weeks old, because Temporal ran in
`start-dev` without persistence and every restart killed in-flight workflows without anyone calling
`engine.finalizar`.

The refusal tests matter as much as the success one: a routine that closes executions by deduction,
running hourly, turns "Temporal blinked" into "all reports failed" if it confuses silence with an
answer.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from cflow_core.execucao import reconciliacao as rec

pytestmark = pytest.mark.integration


class _RPCErroFalso(Exception):
    """Imitates `temporalio.service.RPCError` (which requires positional gRPC arguments)."""

    def __init__(self, status):
        super().__init__("fake")
        self.status = status


async def _semear_execucao(engine, *, idade_minutos: int, com_no: bool = True) -> uuid.UUID:
    """Create a pipeline + `executando` execution with `iniciada_em` in the past. Returns the id."""
    eid, pid = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO pipelines (id, nome, versao_publicada_id) VALUES (:p, :n, NULL)"),
            {"p": pid, "n": f"recon-{pid}"},
        )
        await conn.execute(
            text(
                "INSERT INTO execucoes (id, pipeline_id, versao_numero, status, iniciada_em) "
                "VALUES (:e, :p, 1, 'executando', now() - make_interval(mins => :m))"
            ),
            {"e": eid, "p": pid, "m": idade_minutos},
        )
        if com_no:
            await conn.execute(
                text(
                    "INSERT INTO execucao_nos (id, execucao_id, no_id, status) "
                    "VALUES (:i, :e, 'n1', 'executando')"
                ),
                {"i": uuid.uuid4(), "e": eid},
            )
    return eid


async def _status(engine, eid: uuid.UUID) -> tuple[str, str | None, str | None]:
    async with engine.connect() as conn:
        r = (
            await conn.execute(
                text("SELECT status::text, erro FROM execucoes WHERE id = :e"), {"e": eid}
            )
        ).first()
        n = (
            await conn.execute(
                text("SELECT status::text FROM execucao_nos WHERE execucao_id = :e"), {"e": eid}
            )
        ).first()
    return r[0], r[1], (n[0] if n else None)


def _fingir_temporal(monkeypatch, *, veredito=None, erro_ao_conectar=None, erro_ao_consultar=None):
    """Replace the Temporal connection and query. No network in the test."""

    async def _conectar(_host):
        if erro_ao_conectar:
            raise erro_ao_conectar
        return object()

    monkeypatch.setattr(rec, "conectar", _conectar)

    async def consultar(_cliente, _eid, **_kw):
        if erro_ao_consultar:
            raise erro_ao_consultar
        return veredito

    monkeypatch.setattr(rec, "consultar_uma", consultar)


async def test_fecha_execucao_que_o_temporal_nao_conhece(owner_engine, monkeypatch):
    eid = await _semear_execucao(owner_engine, idade_minutos=120)
    _fingir_temporal(monkeypatch, veredito=rec.Veredito(viva=False, desconhecida=True))

    r = await rec.reconciliar_execucoes_orfas(idade_minima_minutos=30)

    assert r["fechadas"] == 1 and r["vivas"] == 0
    status, erro, status_no = await _status(owner_engine, eid)
    assert status == "falha"
    assert "orchestrator no longer knows" in erro  # readable reason, not an internal code
    assert (
        status_no == "falha"
    )  # the pending node closes too, otherwise the UI shows a "running" node


async def test_nao_toca_no_que_esta_rodando(owner_engine, monkeypatch):
    """Age alone is never a reason: slow pipelines are normal."""
    eid = await _semear_execucao(owner_engine, idade_minutos=60 * 24 * 7)  # one week
    _fingir_temporal(monkeypatch, veredito=rec.Veredito(viva=True, desconhecida=False))

    r = await rec.reconciliar_execucoes_orfas(idade_minima_minutos=30)

    assert r["fechadas"] == 0 and r["vivas"] == 1
    assert (await _status(owner_engine, eid))[0] == "executando"


async def test_temporal_fora_do_ar_nao_fecha_nada(owner_engine, monkeypatch):
    """The guard that defines the routine: orchestrator silence is not a verdict.

    Without it, a Temporal restarting during the hourly pass would mark EVERY in-flight execution
    as failed, and an execution marked failed does not come back on its own.
    """
    eid = await _semear_execucao(owner_engine, idade_minutos=120)
    _fingir_temporal(monkeypatch, erro_ao_conectar=ConnectionRefusedError("no route"))

    r = await rec.reconciliar_execucoes_orfas(idade_minima_minutos=30)

    assert r["fechadas"] == 0 and "orchestrator unreachable" in r["recusado"]
    assert r["candidatas"] == 1  # saw the candidate and still did not touch it
    assert (await _status(owner_engine, eid))[0] == "executando"


async def test_falha_ao_consultar_uma_execucao_nao_fecha_essa_execucao(owner_engine, monkeypatch):
    """Connected, but the query for that execution blew up: skip, conclude nothing."""
    eid = await _semear_execucao(owner_engine, idade_minutos=120)
    _fingir_temporal(monkeypatch, erro_ao_consultar=TimeoutError("deadline"))

    r = await rec.reconciliar_execucoes_orfas(idade_minima_minutos=30)

    assert r["fechadas"] == 0 and r["inconclusivas"] == 1
    assert (await _status(owner_engine, eid))[0] == "executando"


async def test_execucao_recem_iniciada_fica_de_fora(owner_engine, monkeypatch):
    """Race window: the row exists in `execucoes` before the workflow exists in Temporal."""
    eid = await _semear_execucao(owner_engine, idade_minutos=1)
    _fingir_temporal(monkeypatch, veredito=rec.Veredito(viva=False, desconhecida=True))

    r = await rec.reconciliar_execucoes_orfas(idade_minima_minutos=30)

    assert r["fechadas"] == 0
    assert (await _status(owner_engine, eid))[0] == "executando"


async def test_execucao_inline_nao_e_reconciliada(owner_engine, monkeypatch):
    """`CFLOW_USAR_TEMPORAL=false`: there is no orchestrator, hence no possible answer."""
    eid = await _semear_execucao(owner_engine, idade_minutos=120)
    from cflow_core.config import get_settings

    monkeypatch.setattr(get_settings(), "usar_temporal", False)

    r = await rec.reconciliar_execucoes_orfas(idade_minima_minutos=30)

    assert r["fechadas"] == 0 and "inline" in r["recusado"]
    assert (await _status(owner_engine, eid))[0] == "executando"


async def test_fechar_orfa_entra_na_trilha_como_acao_do_sistema(owner_engine, monkeypatch):
    """The audit rule applies to writes made by a routine, not only by people."""
    eid = await _semear_execucao(owner_engine, idade_minutos=120)
    _fingir_temporal(monkeypatch, veredito=rec.Veredito(viva=False, desconhecida=True))

    await rec.reconciliar_execucoes_orfas(idade_minima_minutos=30)

    async with owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT ator_tipo::text, ator_id, acao::text, antes, depois FROM auditoria "
                    "WHERE recurso_tipo = 'execucao' AND recurso_id = :e"
                ),
                {"e": eid},
            )
        ).first()
    assert row is not None, "closing an execution is a relevant write and must leave a trail"
    assert row[0] == "sistema" and row[1] is None
    assert row[3]["status"] == "executando" and row[4]["status"] == "falha"


async def test_workflow_terminado_sem_desfecho_tambem_fecha(owner_engine, monkeypatch):
    """Ended in Temporal and the row stayed `executando`, so `finalizar` did not run.

    Whatever the outcome there, no result exists here: close as failure.
    """
    eid = await _semear_execucao(owner_engine, idade_minutos=120)
    _fingir_temporal(
        monkeypatch,
        veredito=rec.Veredito(
            viva=False, desconhecida=False, detalhe="workflow ended as TERMINATED"
        ),
    )

    r = await rec.reconciliar_execucoes_orfas(idade_minima_minutos=30)

    assert r["fechadas"] == 1
    status, erro, _ = await _status(owner_engine, eid)
    assert status == "falha" and "TERMINATED" in erro


async def test_consultar_distingue_nao_encontrado_de_erro_de_transporte(monkeypatch):
    """The heart of the guard, at the level of the function that talks to Temporal.

    `consultar_uma` MUST propagate transport errors (so the caller skips the execution) and only
    return `desconhecida` when Temporal answered NOT_FOUND for both id formats.
    """
    from temporalio.service import RPCStatusCode

    class _Handle:
        def __init__(self, exc):
            self._exc = exc

        async def describe(self):
            raise self._exc

    class _Cliente:
        def __init__(self, exc):
            self._exc = exc

        def get_workflow_handle(self, _id):
            return _Handle(self._exc)

    import temporalio.service as ts

    monkeypatch.setattr(ts, "RPCError", _RPCErroFalso)
    monkeypatch.setattr("temporalio.service.RPCError", _RPCErroFalso)

    v = await rec.consultar_uma(_Cliente(_RPCErroFalso(RPCStatusCode.NOT_FOUND)), uuid.uuid4())
    assert v.desconhecida is True and v.viva is False

    with pytest.raises(_RPCErroFalso):
        await rec.consultar_uma(_Cliente(_RPCErroFalso(RPCStatusCode.UNAVAILABLE)), uuid.uuid4())


async def test_consultar_membro_de_cadeia_acha_o_dag_da_raiz(monkeypatch):
    """A chain member runs inside `dag-<root>`: `exec-<member>` and `dag-<member>` never existed.
    Asking only for them yields "unknown" for a LIVE member, which is the defect."""
    import temporalio.service as ts
    from temporalio.client import WorkflowExecutionStatus
    from temporalio.service import RPCStatusCode

    monkeypatch.setattr(ts, "RPCError", _RPCErroFalso)
    membro, raiz = uuid.uuid4(), uuid.uuid4()
    pedidos: list[str] = []

    class _Desc:
        status = WorkflowExecutionStatus.RUNNING

    class _Handle:
        def __init__(self, wid):
            self.wid = wid

        async def describe(self):
            pedidos.append(self.wid)
            if self.wid == f"dag-{raiz}":
                return _Desc()
            raise _RPCErroFalso(RPCStatusCode.NOT_FOUND)

    class _Cliente:
        def get_workflow_handle(self, wid):
            return _Handle(wid)

    assert (await rec.consultar_uma(_Cliente(), membro)).desconhecida is True
    v = await rec.consultar_uma(_Cliente(), membro, raiz=raiz)
    assert v.viva is True and pedidos[-1] == f"dag-{raiz}"


async def test_membro_de_cadeia_vivo_no_dag_da_raiz_nao_e_fechado(owner_engine, monkeypatch):
    """Measured with 5 simultaneous chains: 4 failed because reconciliation closed as orphan the
    member pre-created over 30 min earlier while the root's DAG was running."""
    cadeia = uuid.uuid4()
    raiz = await _semear_execucao(owner_engine, idade_minutos=5)
    membro = await _semear_execucao(owner_engine, idade_minutos=45)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE execucoes SET cadeia_id = :c, cadeia_raiz = (id = :r) WHERE id IN (:r, :m)"
            ),
            {"c": cadeia, "r": raiz, "m": membro},
        )
    perguntas: list[tuple] = []

    async def _conectar(_host):
        return object()

    async def consultar(_cliente, eid, raiz=None):
        perguntas.append((eid, raiz))
        return rec.Veredito(viva=raiz is not None, desconhecida=raiz is None)

    monkeypatch.setattr(rec, "conectar", _conectar)
    monkeypatch.setattr(rec, "consultar_uma", consultar)

    r = await rec.reconciliar_execucoes_orfas(idade_minima_minutos=30)

    assert perguntas == [(membro, raiz)]  # the root, at 5 min, is left out by age
    assert r["fechadas"] == 0 and r["vivas"] == 1
    assert (await _status(owner_engine, membro))[0] == "executando"


async def test_simular_pergunta_tudo_e_nao_escreve_nada(owner_engine, monkeypatch):
    """The dry run decides by the SAME criterion as the real one, otherwise it cannot authorize it."""
    eid = await _semear_execucao(owner_engine, idade_minutos=120)
    _fingir_temporal(monkeypatch, veredito=rec.Veredito(viva=False, desconhecida=True))

    r = await rec.reconciliar_execucoes_orfas(idade_minima_minutos=30, simular=True)

    assert r["simulado"] is True and r["fechadas"] == 1  # says what it WOULD do
    assert r["detalhes"][0]["veredito"] == "orphan"
    assert (await _status(owner_engine, eid))[0] == "executando"  # and did not do it

    r2 = await rec.reconciliar_execucoes_orfas(idade_minima_minutos=30)
    assert r2["fechadas"] == 1
    assert (await _status(owner_engine, eid))[0] == "falha"  # same verdict, now applied


async def test_fecha_no_pendente_de_execucao_ja_terminada(owner_engine):
    """A `executando` node inside an execution that already failed.

    Workflow-based reconciliation does not reach this case (it scans `executando` executions, and
    this one already had an outcome), and the detail view showed a "running" node inside something
    that is over.
    """
    eid = await _semear_execucao(owner_engine, idade_minutos=120)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE execucoes SET status='falha', concluida_em = now() - interval '1 hour' "
                "WHERE id = :e"
            ),
            {"e": eid},
        )

    assert await rec.fechar_nos_pendentes(simular=True) == 1
    assert (await _status(owner_engine, eid))[2] == "executando"  # simular writes nothing

    assert await rec.fechar_nos_pendentes() == 1
    assert (await _status(owner_engine, eid))[2] == "falha"


async def test_no_pendente_de_execucao_recem_terminada_espera(owner_engine):
    """Race guard: `finalizar` may be writing the last nodes right now."""
    eid = await _semear_execucao(owner_engine, idade_minutos=120)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE execucoes SET status='concluida', concluida_em = now() WHERE id = :e"),
            {"e": eid},
        )

    assert await rec.fechar_nos_pendentes(atraso_minutos=5) == 0
    assert (await _status(owner_engine, eid))[2] == "executando"


async def test_no_de_execucao_viva_nao_e_tocado(owner_engine):
    eid = await _semear_execucao(owner_engine, idade_minutos=120)
    assert await rec.fechar_nos_pendentes() == 0
    assert (await _status(owner_engine, eid))[2] == "executando"


def test_janela_de_corrida_tem_folga_de_ordens_de_grandeza():
    """The default must not come near the time between writing the row and starting the workflow."""
    assert timedelta(minutes=30) > timedelta(seconds=60)


async def test_trava_faz_so_uma_replica_reconciliar_por_vez(monkeypatch):
    """Two worker replicas on the same tick: one reconciles, the other returns `pulado`.

    Without the lock both would run the SAME `DescribeWorkflow` round: harmless for the database
    (closing is idempotent), but double the load on the orchestrator exactly when it may be
    coming back.
    """
    import asyncio

    entrou = asyncio.Event()
    solta = asyncio.Event()
    chamadas = 0

    async def _lenta(*, idade_minima_minutos):
        nonlocal chamadas
        chamadas += 1
        entrou.set()
        await solta.wait()
        return {"fechadas": 0, "vivas": 0}

    monkeypatch.setattr(rec, "reconciliar_execucoes_orfas", _lenta)

    primeira = asyncio.create_task(rec.reconciliar_com_trava(idade_minima_minutos=30))
    await entrou.wait()  # the 1st is inside, holding the lock
    segunda = await rec.reconciliar_com_trava(idade_minima_minutos=30)
    solta.set()
    r1 = await primeira

    assert segunda == {"pulado": True, "fechadas": 0, "vivas": 0}
    assert r1 == {"fechadas": 0, "vivas": 0} and chamadas == 1

    # The lock belongs to the transaction: released on exit, and the next round enters normally.
    solta.set()
    assert (await rec.reconciliar_com_trava(idade_minima_minutos=30)) == {
        "fechadas": 0,
        "vivas": 0,
    }
