"""A SCHEDULED run is CHAINED.

The gap these tests lock down: `POST /pipelines/{id}/executar` already resolves a DAG of
pipelines, but the SCHEDULER's firing did not: it created ONE execution of the scheduled pipeline
and sent a plain `ExecutarPipeline`. Without the freshness map `{pipeline_id -> execucao_id}`, a
`pipeline` source falls back to "latest completed execution, any" (`execucao/snapshots.py`) and the
monthly run would come out internally inconsistent (plausible and wrong) or would fail with
`sem_execucao` if the upstream had never run. Two semantics for the SAME operation, and the
automatic one was the wrong one.

Covers, in this order:
1. freshness: the consumer reads the snapshot produced IN THAT run, not the previous one. The
   upstream's previous execution has its `concluida_em` pushed into the future ON PURPOSE, so it
   wins the "latest completed" fallback and the test fails if `fontes_execucao` disappears
   (otherwise it would only prove chaining: the chain's run would also be the latest completed,
   and both paths would yield the same snapshot);
2. degeneration: scheduling a pipeline WITHOUT dependencies keeps today's behavior (one execution,
   `origem='agendado'`, null `cadeia_id`, an undeclared parameter is still an error);
3. trail: the scheduled firing is audited, one row per execution in the chain;
4. failed upstream: the WHOLE chain fails and the downstream does not run half-done;
5. refusing `segredo: true` on a schedule still holds, now over the whole CHAIN;
6. `POST /agendamentos/{id}/executar-agora` (the button that tests a schedule) chains the same way;
7. the Temporal path fires `ExecutarDAG` (not `ExecutarPipeline`) and reaches the same freshness.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from cflow_core.execucao import agendador, disparo

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
async def _publicar(cliente, h, nome: str, definicao: dict) -> str:
    rc = await cliente.post("/pipelines", json={"nome": nome}, headers=h)
    assert rc.status_code in (200, 201), rc.text
    pid = rc.json()["id"]
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code in (200, 204), r.text
    r = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    assert r.status_code in (200, 201, 204), r.text
    return pid


async def _republicar(cliente, h, pid: str, definicao: dict, base: int = 2) -> None:
    """Publishes a NEW version of the pipeline (the upstream's data changes between the two runs).

    `base` is the current number: `_publicar` leaves the pipeline at 2 (draft 1 -> published 2)."""
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": base}, headers=h
    )
    assert r.status_code in (200, 204), r.text
    r = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": base + 1, "nota": "v2"}, headers=h
    )
    assert r.status_code in (200, 201, 204), r.text


def _fonte_de(a_id: str, no_id: str = "saida", ident: str = "src") -> dict:
    return {
        "id": ident,
        "tipo": "fonte",
        "config": {"origem": "pipeline", "pipeline_id": a_id, "no_id": no_id},
    }


async def _agendar(cliente, h, pid: str, **corpo) -> str:
    corpo.setdefault("agenda", {"tipo": "horaria", "a_cada_horas": 1})
    r = await cliente.post(f"/pipelines/{pid}/agendamentos", json=corpo, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _venceu(owner_engine, aid: str) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE agendamentos SET proxima_em = now() - interval '1 minute' WHERE id = :id"),
            {"id": uuid.UUID(aid)},
        )


async def _agendamento(app_engine, aid: str) -> dict:
    async with app_engine.connect() as conn:
        return dict(
            (
                await conn.execute(
                    text(
                        "SELECT ultimo_status, ultima_execucao_id FROM agendamentos WHERE id = :id"
                    ),
                    {"id": uuid.UUID(aid)},
                )
            )
            .mappings()
            .one()
        )


async def _execucoes(app_engine, pid: str) -> list[dict]:
    async with app_engine.connect() as conn:
        return [
            dict(r)
            for r in (
                await conn.execute(
                    text(
                        "SELECT id, status, origem, agendamento_id, cadeia_id, versao_numero "
                        "FROM execucoes WHERE pipeline_id = :p ORDER BY iniciada_em"
                    ),
                    {"p": uuid.UUID(pid)},
                )
            )
            .mappings()
            .all()
        ]


async def _iniciador_temporal():
    """Same starter as the production worker, pointed at the tests' isolated queue."""
    from temporalio.client import Client

    from cflow_core.config import get_settings

    cfg = get_settings()
    cli = await Client.connect(cfg.temporal_host)
    disparados: list[str] = []

    async def _iniciar(nome: str, args: list, workflow_id: str) -> None:
        disparados.append(nome)
        await cli.start_workflow(
            nome, args=args, id=workflow_id, task_queue=cfg.temporal_task_queue
        )

    return _iniciar, disparados


# ---------------------------------------------------------------------------
# 1. Freshness: the consumer reads the snapshot of THAT run, not the previous one.
# ---------------------------------------------------------------------------
async def test_agendado_encadeado_le_snapshot_fresco(owner_engine, app_engine, cliente):
    ator, h = await usuario_logado(
        owner_engine, cliente, email="agenc1@example.com", perfil="especialista"
    )
    a_id = await _publicar(
        cliente,
        h,
        "enc-A",
        {
            "nos": [
                {"id": "saida", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1),(2),(3)) t(n)"}
            ],
            "arestas": [],
        },
    )
    b_id = await _publicar(
        cliente,
        h,
        "enc-B",
        {
            "nos": [
                _fonte_de(a_id),
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )
    # Run 1 (manual): A produces 3 rows; this is the "old" snapshot.
    r = await cliente.post(f"/pipelines/{a_id}/executar", headers=h)
    assert r.status_code == 200, r.text
    velha = r.json()["id"]
    det = await aguardar_execucao(cliente, velha, h)
    assert det["status"] == "concluida"
    # The OLD execution becomes, FOREVER, the winner of the "latest completed" fallback
    # (`snapshots.resolver_snapshot` orders by `concluida_em DESC`). Without this the test would
    # only prove CHAINING: once the chain ran, its execution would also be the latest completed
    # and the fallback would return the SAME snapshot, so the test would pass with
    # `fontes_execucao` removed. With the clock poisoned, only the freshness map makes the consumer
    # read this run's data.
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE execucoes SET concluida_em = now() + interval '1 hour' WHERE id = :id"),
            {"id": uuid.UUID(velha)},
        )

    # The upstream's data CHANGES: v2 of A produces 5 rows.
    await _republicar(
        cliente,
        h,
        a_id,
        {
            "nos": [
                {
                    "id": "saida",
                    "tipo": "sql",
                    "sql": "SELECT * FROM (VALUES (1),(2),(3),(4),(5)) t(n)",
                }
            ],
            "arestas": [],
        },
        base=2,
    )

    aid = await _agendar(cliente, h, b_id)
    await _venceu(owner_engine, aid)
    assert await agendador.processar_devidos(disparo.montar_dispatch()) == 1

    ag = await _agendamento(app_engine, aid)
    assert ag["ultimo_status"] == "disparado"
    # A ran through the schedule (2nd execution, on v2) BEFORE B.
    execs_a = await _execucoes(app_engine, a_id)
    assert len(execs_a) == 2, execs_a
    assert execs_a[1]["origem"] == "agendado"
    assert execs_a[1]["versao_numero"] > execs_a[0]["versao_numero"]  # ran v2, the published one

    # And B read the NEW snapshot (5), not the 3-row one from the manual run. This is the proof of
    # the gap: without the freshness map, the pipeline source would fall back to "A's latest
    # completed execution".
    exec_b = str(ag["ultima_execucao_id"])
    det_b = await aguardar_execucao(cliente, exec_b, h)
    assert det_b["status"] == "concluida", det_b
    linhas = {n["no_id"]: n["linhas"] for n in det_b["nos"]}
    assert linhas["src"] == 5, f"read A's old snapshot: {linhas}"

    # The schedule's `ultima_execucao_id` is the ROOT (B), never an upstream.
    execs_b = await _execucoes(app_engine, b_id)
    assert [str(e["id"]) for e in execs_b] == [exec_b]
    assert str(execs_b[0]["agendamento_id"]) == aid
    assert execs_b[0]["cadeia_id"] is not None
    assert str(execs_a[1]["cadeia_id"]) == str(execs_b[0]["cadeia_id"])


# ---------------------------------------------------------------------------
# 2. Degeneration: a pipeline WITHOUT dependencies does not change behavior.
# ---------------------------------------------------------------------------
async def test_agendado_sem_dependencia_degenera(owner_engine, app_engine, cliente):
    """Existing schedules are ALL like this (single-node pipelines). None may change: one
    execution, `origem='agendado'`, NULL `cadeia_id` (a solo execution has no chain) and an
    undeclared parameter is still an ERROR; it must not be silently discarded."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="agenc2@example.com", perfil="especialista"
    )
    pid = await _publicar(
        cliente,
        h,
        "enc-solo",
        {"nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}], "arestas": []},
    )
    aid = await _agendar(cliente, h, pid)
    await _venceu(owner_engine, aid)
    assert await agendador.processar_devidos(disparo.montar_dispatch()) == 1

    execs = await _execucoes(app_engine, pid)
    assert len(execs) == 1
    assert execs[0]["status"] == "concluida"
    assert execs[0]["origem"] == "agendado"
    assert str(execs[0]["agendamento_id"]) == aid
    assert execs[0]["cadeia_id"] is None  # solo is not a chain

    ag = await _agendamento(app_engine, aid)
    assert ag["ultimo_status"] == "disparado"
    assert str(ag["ultima_execucao_id"]) == str(execs[0]["id"])

    # proxima_em rescheduled -> does not fire again.
    assert await agendador.processar_devidos(disparo.montar_dispatch()) == 0

    # An UNDECLARED parameter is still an error (the per-pipeline slicing of the chain must not
    # become a silent discard in the solo case: there is no other pipeline the parameter could
    # belong to).
    pid2 = await _publicar(
        cliente,
        h,
        "enc-solo-typo",
        {"nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}], "arestas": []},
    )
    aid2 = await _agendar(cliente, h, pid2, parametros={"nao_declarado": 1})
    await _venceu(owner_engine, aid2)
    assert await agendador.processar_devidos(disparo.montar_dispatch()) == 1
    ag2 = await _agendamento(app_engine, aid2)
    assert ag2["ultimo_status"] == "erro"
    assert await _execucoes(app_engine, pid2) == []


# ---------------------------------------------------------------------------
# 3. Audit trail of the scheduled firing (one row per execution in the chain).
# ---------------------------------------------------------------------------
async def test_agendado_encadeado_registra_trilha(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="agenc3@example.com", perfil="especialista"
    )
    a_id = await _publicar(
        cliente,
        h,
        "trilha-A",
        {"nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT 1 AS n"}], "arestas": []},
    )
    b_id = await _publicar(
        cliente,
        h,
        "trilha-B",
        {
            "nos": [
                _fonte_de(a_id),
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )
    aid = await _agendar(cliente, h, b_id)
    await _venceu(owner_engine, aid)
    assert await agendador.processar_devidos(disparo.montar_dispatch()) == 1

    exec_a = (await _execucoes(app_engine, a_id))[0]
    exec_b = (await _execucoes(app_engine, b_id))[0]
    async with app_engine.connect() as conn:
        linhas = {
            str(r["recurso_id"]): r
            for r in (
                await conn.execute(
                    text(
                        "SELECT recurso_id, acao, ator_id, depois FROM auditoria "
                        "WHERE recurso_tipo = 'execucao' AND recurso_id = ANY(:ids)"
                    ),
                    {"ids": [exec_a["id"], exec_b["id"]]},
                )
            )
            .mappings()
            .all()
        }
    assert set(linhas) == {str(exec_a["id"]), str(exec_b["id"])}, linhas
    trilha_a = linhas[str(exec_a["id"])]
    trilha_b = linhas[str(exec_b["id"])]
    assert trilha_a["acao"] == "executar" and trilha_b["acao"] == "executar"
    # `origem` marks BOTH (the firing was the schedule); `agendamento_id` only the ROOT, which is
    # the execution the schedule asked for and the one the schedule screen points at.
    assert trilha_a["depois"]["origem"] == "agendado"
    assert trilha_b["depois"]["origem"] == "agendado"
    assert trilha_b["depois"]["agendamento_id"] == aid
    assert trilha_a["depois"]["agendamento_id"] is None
    assert trilha_a["depois"]["cadeia_id"] == trilha_b["depois"]["cadeia_id"] is not None


# ---------------------------------------------------------------------------
# 4. Failed upstream: the WHOLE chain fails; the downstream does not run half-done.
# ---------------------------------------------------------------------------
async def test_agendado_encadeado_upstream_falho_aborta_cadeia(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="agenc4@example.com", perfil="especialista"
    )
    a_id = await _publicar(
        cliente,
        h,
        "falha-A",
        {
            "nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT * FROM tabela_que_nao_existe"}],
            "arestas": [],
        },
    )
    b_id = await _publicar(
        cliente,
        h,
        "falha-B",
        {
            "nos": [
                _fonte_de(a_id),
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )
    aid = await _agendar(cliente, h, b_id)
    await _venceu(owner_engine, aid)
    assert await agendador.processar_devidos(disparo.montar_dispatch()) == 1

    ag = await _agendamento(app_engine, aid)
    assert ag["ultimo_status"] == "erro"
    assert ag["ultima_execucao_id"] is None
    execs_a = await _execucoes(app_engine, a_id)
    assert len(execs_a) == 1 and execs_a[0]["status"] == "falha"
    # No consumer execution left hanging in 'executando' (a half-done run that lies on screen):
    # either it does not exist or it is marked as failed.
    assert all(e["status"] == "falha" for e in await _execucoes(app_engine, b_id))


# ---------------------------------------------------------------------------
# 5. `segredo: true` is still refused on a schedule, now over the CHAIN.
# ---------------------------------------------------------------------------
async def test_agendar_com_segredo_de_upstream_e_recusado(owner_engine, cliente):
    """The rule: scheduling with a `segredo: true` parameter is refused, because the schedule would
    store the value in clear text in `agendamentos.parametros`. With chained firing, the parameter
    now REACHES the upstream that declares it; if the refusal looked only at the root, the
    credential would be stored and would now also be used."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="agenc5@example.com", perfil="especialista"
    )
    a_id = await _publicar(
        cliente,
        h,
        "seg-A",
        {
            "parametros": [{"nome": "senha_api", "tipo": "texto", "segredo": True}],
            "nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT 1 AS n"}],
            "arestas": [],
        },
    )
    b_id = await _publicar(
        cliente,
        h,
        "seg-B",
        {
            "nos": [
                _fonte_de(a_id),
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )
    r = await cliente.post(
        f"/pipelines/{b_id}/agendamentos",
        json={
            "agenda": {"tipo": "horaria", "a_cada_horas": 1},
            "parametros": {"senha_api": "do-not-store-this"},
        },
        headers=h,
    )
    assert r.status_code == 400, r.text
    assert r.json()["erro"]["codigo"] == "agendamento_com_segredo"


# ---------------------------------------------------------------------------
# 6. The "Run now" button chains the same way as the schedule it tests.
# ---------------------------------------------------------------------------
async def test_executar_agora_encadeia(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="agenc6@example.com", perfil="especialista"
    )
    a_id = await _publicar(
        cliente,
        h,
        "agora-A",
        {
            "nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1),(2)) t(n)"}],
            "arestas": [],
        },
    )
    b_id = await _publicar(
        cliente,
        h,
        "agora-B",
        {
            "nos": [
                _fonte_de(a_id),
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )
    aid = await _agendar(cliente, h, b_id, agenda={"tipo": "diaria", "horario": "23:59"})
    r = await cliente.post(f"/agendamentos/{aid}/executar-agora", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "disparado", r.text

    # A ran (it had never run): without chaining, B would fail with `sem_execucao`.
    execs_a = await _execucoes(app_engine, a_id)
    assert len(execs_a) == 1 and execs_a[0]["origem"] == "agendado"
    det = await aguardar_execucao(cliente, r.json()["execucao_id"], h)
    assert det["status"] == "concluida", det
    assert {n["no_id"]: n["linhas"] for n in det["nos"]}["src"] == 2


# ---------------------------------------------------------------------------
# 7. Temporal path: fires `ExecutarDAG`, not `ExecutarPipeline`.
# ---------------------------------------------------------------------------
async def test_agendado_encadeado_via_temporal(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="agenc7@example.com", perfil="especialista"
    )
    a_id = await _publicar(
        cliente,
        h,
        "tmp-A",
        {
            "nos": [
                {"id": "saida", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1),(2),(3)) t(n)"}
            ],
            "arestas": [],
        },
    )
    b_id = await _publicar(
        cliente,
        h,
        "tmp-B",
        {
            "nos": [
                _fonte_de(a_id),
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )
    # Same poisoning as test 1: a previous run of A (3 rows) keeps a `concluida_em` in the future,
    # so the "latest completed" fallback would always pick it. Only the `fontes_execucao` that
    # `ExecutarDAG` receives pre-built makes B read this run's snapshot (5 rows).
    r = await cliente.post(f"/pipelines/{a_id}/executar", headers=h)
    velha = r.json()["id"]
    assert (await aguardar_execucao(cliente, velha, h))["status"] == "concluida"
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE execucoes SET concluida_em = now() + interval '1 hour' WHERE id = :id"),
            {"id": uuid.UUID(velha)},
        )
    await _republicar(
        cliente,
        h,
        a_id,
        {
            "nos": [
                {
                    "id": "saida",
                    "tipo": "sql",
                    "sql": "SELECT * FROM (VALUES (1),(2),(3),(4),(5)) t(n)",
                }
            ],
            "arestas": [],
        },
        base=2,
    )

    iniciar, disparados = await _iniciador_temporal()
    aid = await _agendar(cliente, h, b_id)
    await _venceu(owner_engine, aid)
    assert await agendador.processar_devidos(disparo.montar_dispatch(iniciar)) == 1
    assert disparados == ["ExecutarDAG"], disparados

    ag = await _agendamento(app_engine, aid)
    det = await aguardar_execucao(cliente, str(ag["ultima_execucao_id"]), h)
    assert det["status"] == "concluida", det
    assert {n["no_id"]: n["linhas"] for n in det["nos"]}["src"] == 5, det


# ---------------------------------------------------------------------------
# 8. Temporal + failed upstream: no pre-created execution stays stuck in 'executando'.
# ---------------------------------------------------------------------------
async def test_agendado_encadeado_temporal_nao_deixa_execucao_presa(
    owner_engine, app_engine, cliente
):
    """On the Temporal path the chain's executions are pre-created BEFORE running (that is what
    provides the freshness map). If an upstream fails, the ones that will never run must be
    finalized: stuck in 'executando' they would show forever a run in progress that nobody is
    executing, and the operator would wait for a result that never comes."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="agenc8@example.com", perfil="especialista"
    )
    a_id = await _publicar(
        cliente,
        h,
        "tmpfalha-A",
        {
            "nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT * FROM nao_existe_mesmo"}],
            "arestas": [],
        },
    )
    b_id = await _publicar(
        cliente,
        h,
        "tmpfalha-B",
        {
            "nos": [
                _fonte_de(a_id),
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )
    iniciar, disparados = await _iniciador_temporal()
    aid = await _agendar(cliente, h, b_id)
    await _venceu(owner_engine, aid)
    assert await agendador.processar_devidos(disparo.montar_dispatch(iniciar)) == 1
    assert disparados == ["ExecutarDAG"]

    ag = await _agendamento(app_engine, aid)
    det = await aguardar_execucao(cliente, str(ag["ultima_execucao_id"]), h)
    assert det["status"] == "falha", det
    assert all(e["status"] == "falha" for e in await _execucoes(app_engine, a_id))
    assert all(e["status"] == "falha" for e in await _execucoes(app_engine, b_id))
