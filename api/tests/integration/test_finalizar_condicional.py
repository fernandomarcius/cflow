"""`engine.finalizar` is a blind UPDATE, and chain cleanup sweeps executions it did not create.

`ExecutarDAG` PRE-CREATES one execution per pipeline in the chain (the freshness map has to be
complete before any node runs). When a level fails, the following levels will never run and must be
closed, otherwise they stay `executando` forever and the panel shows a run in progress that nobody
is executing.

The risk of closing by sweep is the opposite: rewriting as `falha` a sibling that already
COMPLETED. That swaps a good snapshot for a lying outcome, on screen and in the audit trail, which
is permanent. Hence `somente_se_executando`.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from cflow_core.execucao import engine

pytestmark = pytest.mark.integration


async def _semear_execucao(owner_engine, status: str) -> uuid.UUID:
    """Create a raw execution in the requested status. Deliberately bypasses `criar_execucao`: what
    is tested here is the closing write, not the context assembly."""
    from ._arnes_auth import semear_usuario

    eid = uuid.uuid4()
    pid = uuid.uuid4()
    uid = await semear_usuario(owner_engine, email=f"fin-{eid}@example.com", perfil="admin")
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO pipelines (id, nome, dono_id) VALUES (:p, :n, :u)"),
            {"p": pid, "n": f"fin-{eid}", "u": uid},
        )
        await conn.execute(
            text(
                "INSERT INTO execucoes (id, pipeline_id, versao_numero, status, iniciada_por, "
                "origem, parametros) "
                "VALUES (:i, :p, 1, CAST(:s AS status_execucao), :u, 'manual', '{}'::jsonb)"
            ),
            {"i": eid, "p": pid, "s": status, "u": uid},
        )
    return eid


async def _status(app_engine, eid: uuid.UUID) -> tuple[str, str | None]:
    async with app_engine.connect() as conn:
        linha = (
            (
                await conn.execute(
                    text("SELECT status, erro FROM execucoes WHERE id = :i"), {"i": eid}
                )
            )
            .mappings()
            .first()
        )
    return linha["status"], linha["erro"]


async def test_condicional_nao_toca_execucao_ja_concluida(owner_engine, app_engine):
    """The case that motivates the guard: a sibling that finished OK before the chain fell."""
    eid = await _semear_execucao(owner_engine, "concluida")

    await engine.finalizar(execucao_id=eid, erro="Chain interrupted.", somente_se_executando=True)

    status, erro = await _status(app_engine, eid)
    assert status == "concluida", "cleanup rewrote as failed a sibling that had already completed"
    assert erro is None


async def test_condicional_nao_reescreve_o_motivo_de_quem_ja_falhou(owner_engine, app_engine):
    """An upstream that failed already stored the REAL reason. Replacing it with the chain's generic
    one would erase the only clue to why the run fell."""
    eid = await _semear_execucao(owner_engine, "falha")
    await engine.finalizar(execucao_id=eid, erro="Failed to run the node (SQL or source).")

    await engine.finalizar(execucao_id=eid, erro="Cadeia interrompida.", somente_se_executando=True)

    status, erro = await _status(app_engine, eid)
    assert status == "falha"
    assert erro == "Failed to run the node (SQL or source).", "the real reason was overwritten"


async def test_condicional_fecha_quem_ficou_preso_em_executando(owner_engine, app_engine):
    """What the cleanup exists to solve, including the execution whose child workflow never even
    STARTED, which is in no "already started" list."""
    eid = await _semear_execucao(owner_engine, "executando")

    await engine.finalizar(execucao_id=eid, erro="Cadeia interrompida.", somente_se_executando=True)

    status, erro = await _status(app_engine, eid)
    assert status == "falha"
    assert erro == "Cadeia interrompida."


async def test_sem_o_guard_o_update_continua_cego(owner_engine, app_engine):
    """The default does NOT change: whoever finalizes their own execution writes unconditionally. If
    this test starts failing, someone made the guard implicit, and then the normal completion path
    may silently write nothing."""
    eid = await _semear_execucao(owner_engine, "concluida")

    await engine.finalizar(execucao_id=eid, erro="rewritten on purpose")

    status, erro = await _status(app_engine, eid)
    assert status == "falha"
    assert erro == "rewritten on purpose"


# ---------------------------------------------------------------------------
# The interactive route (Studio's Run button) pre-creates the WHOLE chain before talking to
# Temporal. There is a gap between the two: full queue, dropped client, database blinking. The
# `ExecutarDAG` `try/except` does not reach it: the workflow never started.
# ---------------------------------------------------------------------------
async def test_falha_ao_iniciar_o_dag_pela_rota_nao_deixa_a_cadeia_presa(
    owner_engine, app_engine, cliente, monkeypatch
):
    from ._arnes_auth import usuario_logado

    _, h = await usuario_logado(
        owner_engine, cliente, email="rotadag@example.com", perfil="especialista"
    )

    async def _publicar(nome: str, definicao: dict) -> str:
        rc = await cliente.post("/pipelines", json={"nome": nome}, headers=h)
        assert rc.status_code in (200, 201), rc.text
        pid = rc.json()["id"]
        r = await cliente.put(
            f"/pipelines/{pid}/rascunho",
            json={"definicao": definicao, "base_numero": 0},
            headers=h,
        )
        assert r.status_code in (200, 204), r.text
        r = await cliente.post(
            f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
        )
        assert r.status_code in (200, 201, 204), r.text
        return pid

    a_id = await _publicar(
        "rotadag-A",
        {"nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT 1 AS n"}], "arestas": []},
    )
    b_id = await _publicar(
        "rotadag-B",
        {
            "nos": [
                {
                    "id": "src",
                    "tipo": "fonte",
                    "config": {
                        "origem": "pipeline",
                        "pipeline_id": a_id,
                        "no_id": "saida",
                    },
                },
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )

    from cflow_core.config import get_settings

    cfg = get_settings()
    if not cfg.usar_temporal:
        pytest.skip("path exercised only with CFLOW_USAR_TEMPORAL=true.")

    import temporalio.client as tcli

    async def _recusa(*a, **kw):
        raise RuntimeError("queue full")

    monkeypatch.setattr(tcli.Client, "connect", _recusa)

    r = await cliente.post(f"/pipelines/{b_id}/executar", json={}, headers=h)
    assert r.status_code >= 400, f"the route answered {r.status_code}: {r.text}"

    async with app_engine.connect() as conn:
        presas = (
            await conn.execute(text("SELECT count(*) FROM execucoes WHERE status = 'executando'"))
        ).scalar_one()
    assert presas == 0, f"{presas} execution(s) of the chain stayed stuck in 'executando'"
