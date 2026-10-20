"""Regressions for the chained SCHEDULED firing.

Each test here came from a finding, and the test was written before the fix. They all fire through
the `agendador` (not the route), because each defect only shows up on the automatic path: it is the
overnight run nobody is watching, which is where the original delivery changed the semantics.

The harness (`_publicar`, `_fonte_de`, `_agendar`, `_venceu`, ...) is the same as in
`test_agendamento_encadeado.py`, imported from there so the two suites do not diverge the day the
definition format changes.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest
from sqlalchemy import text

from cflow_core.execucao import agendador, disparo

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao
from .test_agendamento_encadeado import (
    _agendamento,
    _agendar,
    _execucoes,
    _fonte_de,
    _publicar,
    _republicar,
    _venceu,
)

pytestmark = pytest.mark.integration


async def _trilha_agendamento(app_engine, aid: str) -> list[dict]:
    async with app_engine.connect() as conn:
        return [
            dict(r)
            for r in (
                await conn.execute(
                    text(
                        "SELECT acao, ator_tipo, depois FROM auditoria "
                        "WHERE recurso_tipo = 'agendamento' AND recurso_id = :id ORDER BY id"
                    ),
                    {"id": uuid.UUID(aid)},
                )
            )
            .mappings()
            .all()
        ]


async def _pipeline_vazio(cliente, h, nome: str) -> str:
    """Pipeline with NO version at all: the upstream that makes the chain fail to resolve (`sem_definicao`)."""
    return (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]


# ---------------------------------------------------------------------------
# S1 - the `segredo: true` refusal evaluated only in the CRUD can be bypassed by ordering.
# ---------------------------------------------------------------------------
async def test_segredo_declarado_no_upstream_DEPOIS_do_agendamento_e_recusado_no_disparo(
    owner_engine, app_engine, cliente
):
    """The sequence that bypasses the CRUD refusal, in the order it really happens.

    The refusal in `_recusar_segredo_no_agendamento` runs on create/edit. The CHAIN changes later:
    all it takes is for someone to publish an upstream version that declares `senha_api: segredo`.
    The schedule never goes through a CRUD again, and the value already stored in clear text in
    `agendamentos.parametros` starts being DELIVERED to the upstream that now declares it.

    Before chaining this was a loud error (the root did not declare the parameter -> 400
    `parametros_invalidos` -> `ultimo_status='erro'`). Afterwards it became `disparado`: the change
    made the system WORSE. The refusal must also hold at FIRING time.
    """
    _, h = await usuario_logado(
        owner_engine, cliente, email="corr-s1@example.com", perfil="especialista"
    )
    a_id = await _publicar(
        cliente,
        h,
        "s1-A",
        {"nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT 1 AS n"}], "arestas": []},
    )
    b_id = await _publicar(
        cliente,
        h,
        "s1-B",
        {
            "nos": [
                _fonte_de(a_id),
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )
    # 1) Nobody declares `senha_api` yet -> the CRUD accepts it (this is the hole).
    aid = await _agendar(cliente, h, b_id, parametros={"senha_api": "do-not-store-this"})

    # 2) The upstream starts declaring the parameter as a CREDENTIAL. The schedule is not touched.
    await _republicar(
        cliente,
        h,
        a_id,
        {
            "parametros": [{"nome": "senha_api", "tipo": "texto", "segredo": True}],
            "nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT 1 AS n"}],
            "arestas": [],
        },
        base=2,
    )

    # 3) Overnight run. The clear-text value MUST NOT reach the upstream.
    await _venceu(owner_engine, aid)
    assert await agendador.processar_devidos(disparo.montar_dispatch()) == 1
    ag = await _agendamento(app_engine, aid)
    assert ag["ultimo_status"] == "erro", ag
    assert ag["ultima_execucao_id"] is None
    assert await _execucoes(app_engine, a_id) == [], (
        "the clear-text credential reached the upstream"
    )
    assert await _execucoes(app_engine, b_id) == []


# ---------------------------------------------------------------------------
# S2 - the chain runs the PUBLISHED version of the upstream, never the draft open in the editor.
# ---------------------------------------------------------------------------
async def test_cadeia_agendada_roda_versao_publicada_do_upstream_nao_o_rascunho(
    owner_engine, app_engine, cliente
):
    """The execution references the exact published version, never the latest. `resolver_ordem`
    used to resolve the upstream through `ps.obter(pid, None)`, which returns the LATEST version,
    draft included. A half-done draft left open in the editor entered the monthly run, and whoever
    triggers the consumer is not even looking at the upstream."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="corr-s2@example.com", perfil="especialista"
    )
    a_id = await _publicar(
        cliente,
        h,
        "s2-A",
        {
            "nos": [
                {"id": "saida", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1),(2),(3)) t(n)"}
            ],
            "arestas": [],
        },
    )
    # DRAFT (unpublished) on the upstream: 5 rows. This is what must not run.
    r = await cliente.put(
        f"/pipelines/{a_id}/rascunho",
        json={
            "definicao": {
                "nos": [
                    {
                        "id": "saida",
                        "tipo": "sql",
                        "sql": "SELECT * FROM (VALUES (1),(2),(3),(4),(5)) t(n)",
                    }
                ],
                "arestas": [],
            },
            "base_numero": 2,
        },
        headers=h,
    )
    assert r.status_code in (200, 204), r.text

    b_id = await _publicar(
        cliente,
        h,
        "s2-B",
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

    execs_a = await _execucoes(app_engine, a_id)
    assert len(execs_a) == 1
    assert execs_a[0]["versao_numero"] == 2, f"ran the draft (v3): {execs_a}"

    ag = await _agendamento(app_engine, aid)
    det_b = await aguardar_execucao(cliente, str(ag["ultima_execucao_id"]), h)
    assert det_b["status"] == "concluida", det_b
    assert {n["no_id"]: n["linhas"] for n in det_b["nos"]}["src"] == 3, det_b


# ---------------------------------------------------------------------------
# S3 - a chain that does not resolve must not fail without a trace.
# ---------------------------------------------------------------------------
async def test_cadeia_que_nao_resolve_deixa_rastro_na_trilha(owner_engine, app_engine, cliente):
    """The run fails at 01:00 and the schedule screen shows `erro` with nothing to click: zero
    executions (the chain was never created) and, before this fix, zero audit rows. The operator had
    no WAY to find the reason. The record lives in the audit trail because it is the only
    append-only, per-resource queryable thing the platform has: the execution does not exist to
    carry the error, and `ultimo_status` only holds one word."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="corr-s3@example.com", perfil="especialista"
    )
    orfao = await _pipeline_vazio(cliente, h, "s3-sem-versao")
    b_id = await _publicar(
        cliente,
        h,
        "s3-B",
        {
            "nos": [
                _fonte_de(orfao),
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
    linhas = [t for t in await _trilha_agendamento(app_engine, aid) if t["acao"] == "executar"]
    assert linhas, "scheduled firing failed without leaving any trace"
    depois = linhas[-1]["depois"]
    assert depois["status"] == "erro", depois
    # The reason must be READABLE: "sem_definicao" is what tells the operator to publish the upstream.
    assert depois.get("erro_codigo") == "sem_definicao", depois
    assert depois.get("erro")
    # The scheduler loop runs without a human actor (nobody clicked at 01:00).
    assert linhas[-1]["ator_tipo"] == "sistema"


# ---------------------------------------------------------------------------
# S4 - zombie in pre-creation: failure between `criar_execucao` and `start_workflow`.
# ---------------------------------------------------------------------------
async def test_falha_ao_iniciar_workflow_nao_deixa_execucao_presa(
    owner_engine, app_engine, cliente
):
    """Pre-creation exists so the freshness map is complete BEFORE any node runs. If Temporal
    refuses `start_workflow` (full queue, dropped client), the executions already created stay in
    `executando` FOREVER: the `try/except` of `ExecutarDAG` does not reach them because the
    workflow never started. The dashboard would show a run in progress that nobody executes."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="corr-s4@example.com", perfil="especialista"
    )
    a_id = await _publicar(
        cliente,
        h,
        "s4-A",
        {"nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT 1 AS n"}], "arestas": []},
    )
    b_id = await _publicar(
        cliente,
        h,
        "s4-B",
        {
            "nos": [
                _fonte_de(a_id),
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )

    async def _iniciar_que_falha(nome: str, args: list, workflow_id: str) -> None:
        raise RuntimeError("Temporal is down")

    aid = await _agendar(cliente, h, b_id)
    await _venceu(owner_engine, aid)
    assert await agendador.processar_devidos(disparo.montar_dispatch(_iniciar_que_falha)) == 1

    ag = await _agendamento(app_engine, aid)
    assert ag["ultimo_status"] == "erro", ag
    presas = [
        e
        for e in (await _execucoes(app_engine, a_id)) + (await _execucoes(app_engine, b_id))
        if e["status"] == "executando"
    ]
    assert presas == [], f"zombie execution stuck in 'executando': {presas}"


async def test_falha_no_meio_da_pre_criacao_nao_deixa_execucao_presa(
    owner_engine, app_engine, cliente, monkeypatch
):
    """Same hole, one step earlier: the chain's 1st execution was created and the 2nd blew up
    (invalid parameter, database hiccup). The 1st would stay `executando` forever."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="corr-s4b@example.com", perfil="especialista"
    )
    a_id = await _publicar(
        cliente,
        h,
        "s4b-A",
        {"nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT 1 AS n"}], "arestas": []},
    )
    b_id = await _publicar(
        cliente,
        h,
        "s4b-B",
        {
            "nos": [
                _fonte_de(a_id),
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )
    from cflow_core.execucao import engine as _engine

    original = _engine.criar_execucao
    chamadas = {"n": 0}

    async def _criar_que_falha_na_segunda(**kw):
        chamadas["n"] += 1
        if chamadas["n"] == 2:
            raise RuntimeError("database hiccup in the middle of pre-creation")
        return await original(**kw)

    monkeypatch.setattr(_engine, "criar_execucao", _criar_que_falha_na_segunda)

    async def _iniciar(nome: str, args: list, workflow_id: str) -> None:  # never reached
        raise AssertionError("must not start a workflow with an incomplete chain")

    aid = await _agendar(cliente, h, b_id)
    await _venceu(owner_engine, aid)
    assert await agendador.processar_devidos(disparo.montar_dispatch(_iniciar)) == 1

    presas = [
        e
        for e in (await _execucoes(app_engine, a_id)) + (await _execucoes(app_engine, b_id))
        if e["status"] == "executando"
    ]
    assert presas == [], f"zombie execution stuck in 'executando': {presas}"


# ---------------------------------------------------------------------------
# S5 - a parameter that NO pipeline in the chain declares is a loud error, as in the solo case.
# ---------------------------------------------------------------------------
async def test_parametro_que_ninguem_da_cadeia_declara_e_erro(owner_engine, app_engine, cliente):
    """In the solo case, an undeclared name is a 400 `parametros_invalidos` and the schedule ends
    up `erro` (locked by `test_agendado_sem_dependencia_degenera`). In the chain,
    `filtrar_parametros_declarados` discarded it SILENTLY: a typo in `competencia` would produce,
    overnight, a run without the period slice: green, plausible and wrong. That is exactly the
    outcome chaining exists to avoid."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="corr-s5@example.com", perfil="especialista"
    )
    a_id = await _publicar(
        cliente,
        h,
        "s5-A",
        {
            "parametros": [{"nome": "competencia", "tipo": "competencia"}],
            "nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT 1 AS n"}],
            "arestas": [],
        },
    )
    b_id = await _publicar(
        cliente,
        h,
        "s5-B",
        {
            "nos": [
                _fonte_de(a_id),
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )
    # `competencai` is the typo. No pipeline in the chain declares it.
    aid = await _agendar(cliente, h, b_id, parametros={"competencai": "2026-07"})
    await _venceu(owner_engine, aid)
    assert await agendador.processar_devidos(disparo.montar_dispatch()) == 1

    ag = await _agendamento(app_engine, aid)
    assert ag["ultimo_status"] == "erro", ag
    assert await _execucoes(app_engine, b_id) == []
    assert await _execucoes(app_engine, a_id) == []


async def test_parametro_declarado_so_pelo_upstream_continua_valendo(
    owner_engine, app_engine, cliente
):
    """The counter-test of the fix above: a parameter that ONLY the upstream declares (the real
    case: a per-operation parameter, without `competencia`) must keep flowing. Refusing it would
    trade the silent discard for a block that keeps the run from executing."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="corr-s5b@example.com", perfil="especialista"
    )
    a_id = await _publicar(
        cliente,
        h,
        "s5b-A",
        {
            "parametros": [{"nome": "operacao_id", "tipo": "inteiro"}],
            "nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT :operacao_id AS op"}],
            "arestas": [],
        },
    )
    b_id = await _publicar(
        cliente,
        h,
        "s5b-B",
        {
            "nos": [
                _fonte_de(a_id),
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )
    aid = await _agendar(cliente, h, b_id, parametros={"operacao_id": 61})
    await _venceu(owner_engine, aid)
    assert await agendador.processar_devidos(disparo.montar_dispatch()) == 1

    ag = await _agendamento(app_engine, aid)
    assert ag["ultimo_status"] == "disparado", ag
    execs_a = await _execucoes(app_engine, a_id)
    assert len(execs_a) == 1 and execs_a[0]["status"] == "concluida"


# ---------------------------------------------------------------------------
# S6 - per-operation fan-out multiplies the WHOLE CHAIN. Cost MEASURED (not fixed).
# ---------------------------------------------------------------------------
async def test_fanout_multiplica_a_cadeia_inteira_custo_medido(
    owner_engine, app_engine, cliente, capsys
):
    """MEASURES the cost the delivery introduced and which is still OPEN.

    The per-operation fan-out (`@operacoes`) runs a COMPLETE chain per operation. Where there used
    to be zero upstream executions, there are now N, and via Temporal all N are fired without
    waiting for any to finish, hence CONCURRENT.

    This test fixes nothing: it locks the number so the first big nightly run is not the one that
    discovers it. If someone deduplicates the upstream per chain, it fails and forces the declared
    cost to be updated, which is the right behavior."""
    import json as _json

    _, h = await usuario_logado(
        owner_engine, cliente, email="corr-s6@example.com", perfil="especialista"
    )
    a_id = await _publicar(
        cliente,
        h,
        "s6-upstream",
        {"nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT 1 AS n"}], "arestas": []},
    )
    b_id = await _publicar(
        cliente,
        h,
        "s6-raiz",
        {
            "parametros": [{"nome": "operacao_id", "tipo": "inteiro"}],
            "nos": [
                _fonte_de(a_id),
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )
    pgid, pvid = uuid.uuid4(), uuid.uuid4()
    pdef = {
        "blocos": [],
        "execucao": {"operador": [{"tipo": "operacao", "param": "operacao_id"}], "derivacoes": []},
    }
    async with owner_engine.begin() as conn:
        await conn.execute(text("INSERT INTO paginas (id, nome) VALUES (:i, 's6-pg')"), {"i": pgid})
        await conn.execute(
            text(
                "INSERT INTO pagina_versoes (id, pagina_id, numero, definicao, status) "
                "VALUES (:v, :p, 1, CAST(:d AS jsonb), 'publicada')"
            ),
            {"v": pvid, "p": pgid, "d": _json.dumps(pdef)},
        )
        await conn.execute(
            text("UPDATE paginas SET versao_publicada_id = :v WHERE id = :p"),
            {"v": pvid, "p": pgid},
        )

    operacoes = [1, 2, 3]
    aid = await _agendar(
        cliente,
        h,
        b_id,
        pagina_id=str(pgid),
        parametros={"@operacoes": operacoes},
    )
    await _venceu(owner_engine, aid)
    t0 = time.monotonic()
    assert await agendador.processar_devidos(disparo.montar_dispatch()) == 1
    decorrido = time.monotonic() - t0

    execs_a = await _execucoes(app_engine, a_id)
    execs_b = await _execucoes(app_engine, b_id)
    # THE NUMBER: N operations = N upstream executions. Not 1.
    assert len(execs_a) == len(operacoes), execs_a
    assert len(execs_b) == len(operacoes), execs_b
    # And each one in a different CHAIN: nothing is shared between them.
    assert len({str(e["cadeia_id"]) for e in execs_a}) == len(operacoes)

    # CONCURRENCY, measured on the production path: `_disparar` does not wait for any chain to
    # finish, so all N are fired at once. With a starter that only RECORDS (does not run), what is
    # left in the database is exactly what stays in flight simultaneously: N chains, 2N
    # executions pre-created in 'executando' before any node runs.
    pedidos: list[str] = []

    async def _so_registra(nome: str, args: list, workflow_id: str) -> None:
        pedidos.append(nome)

    aid2 = await _agendar(
        cliente, h, b_id, pagina_id=str(pgid), parametros={"@operacoes": operacoes}
    )
    await _venceu(owner_engine, aid2)
    assert await agendador.processar_devidos(disparo.montar_dispatch(_so_registra)) == 1
    assert pedidos == ["ExecutarDAG"] * len(operacoes), pedidos
    em_voo = [
        e
        for e in (await _execucoes(app_engine, a_id)) + (await _execucoes(app_engine, b_id))
        if e["status"] == "executando"
    ]
    assert len(em_voo) == 2 * len(operacoes), em_voo

    n = len(operacoes)
    print(
        f"\n[S6 measured cost] fan-out of {n} operations -> {len(execs_a)} upstream executions "
        f"(factor {n}x, before chaining: 0), {decorrido:.2f}s inline "
        f"({decorrido / n:.3f}s per chain, 1-node pipelines). "
        f"Via Temporal: {len(pedidos)} ExecutarDAG fired without waiting for any to finish and "
        f"{len(em_voo)} executions in 'executando' at the same time: concurrency is N, not 1."
    )


# ---------------------------------------------------------------------------
# MINOR - a sibling STILL RUNNING when another at the same level fails.
# ---------------------------------------------------------------------------
async def test_irmao_em_execucao_nao_fica_preso_quando_o_outro_falha(
    owner_engine, app_engine, cliente, monkeypatch
):
    """Siblings at the same level run in `asyncio.gather`. Without `return_exceptions=True`, the
    first error hands control back while the sibling is STILL running: nobody awaits it anymore and
    its execution stays `executando`, the same zombie as S4 through another door. (Via Temporal it
    is worse: on close, the parent workflow TERMINATES the child still in progress.)

    The slow sibling is made slow in the NODE (`executar_no` with a `sleep`), not with heavy SQL:
    DuckDB runs on the loop itself and blocks, so "big SQL" does not leave the sibling suspended, it
    leaves the loop stopped, and the test would pass by accident with or without the fix."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="corr-m1@example.com", perfil="especialista"
    )
    ruim = await _publicar(
        cliente,
        h,
        "m1-ruim",
        {
            "nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT * FROM does_not_exist_at_all"}],
            "arestas": [],
        },
    )
    lento = await _publicar(
        cliente,
        h,
        "m1-lento",
        {"nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT 1 AS marca_lenta"}], "arestas": []},
    )
    b_id = await _publicar(
        cliente,
        h,
        "m1-raiz",
        {
            "nos": [
                _fonte_de(ruim, ident="s1"),
                _fonte_de(lento, ident="s2"),
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM s1"},
            ],
            "arestas": [{"de": "s1", "para": "cont"}, {"de": "s2", "para": "cont"}],
        },
    )
    from cflow_core.execucao import engine as _engine

    original_no = _engine.executar_no

    async def _no_com_irmao_lento(**kw):
        # Only the "slow" sibling waits: enough for it to be SUSPENDED in an await when the other
        # blows up, which is the situation where a plain `gather` abandons it.
        if "marca_lenta" in json.dumps(kw.get("definicao") or {}):
            await asyncio.sleep(0.4)
        return await original_no(**kw)

    monkeypatch.setattr(_engine, "executar_no", _no_com_irmao_lento)

    aid = await _agendar(cliente, h, b_id)
    await _venceu(owner_engine, aid)
    assert await agendador.processar_devidos(disparo.montar_dispatch()) == 1

    ag = await _agendamento(app_engine, aid)
    assert ag["ultimo_status"] == "erro", ag
    presas = [
        e
        for e in (await _execucoes(app_engine, ruim))
        + (await _execucoes(app_engine, lento))
        + (await _execucoes(app_engine, b_id))
        if e["status"] == "executando"
    ]
    assert presas == [], f"sibling stuck in 'executando': {presas}"


# ---------------------------------------------------------------------------
# MINOR - the author's freshness test would pass WITHOUT the freshness map.
# Fixed IN PLACE, in `test_agendamento_encadeado.py`: tests 1 (inline) and 7 (Temporal) now push the
# `concluida_em` of a previous upstream execution into the FUTURE, which makes the "latest
# completed" fallback (`snapshots.resolver_snapshot`) always pick it. They therefore fail if
# `fontes_execucao` disappears. Repeating the case here would keep two copies of the same test, and
# the one that goes stale is always the one nobody remembers exists.
