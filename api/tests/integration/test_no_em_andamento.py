"""Node IN PROGRESS: the execution records that the node started, not only that it finished.

Before, `execucao_nos` only got a row at the outcome (completed/failed). During a long execution,
the node running at that instant was indistinguishable from one that had not started yet: the
detail wrote "waiting" for both, and the question that brings the user to that screen ("which step
is it on?") had no answer.

The guard test matters as much as the happy path: the start mark must NOT rewrite a node already
`concluida`, because activity re-delivery depends precisely on finding it completed (external
effect idempotency and incremental reuse).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from cflow_core.execucao import engine

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


async def _pipeline_simples(cliente, h) -> str:
    definicao = {
        "nos": [
            {"id": "src", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1),(2)) t(n)"},
            {"id": "fim", "tipo": "sql", "sql": 'SELECT count(*) AS c FROM "src"'},
        ],
        "arestas": [{"de": "src", "para": "fim"}],
    }
    pid = (await cliente.post("/pipelines", json={"nome": "In progress"}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    return pid


async def test_executar_no_registra_quando_o_no_comecou(owner_engine, cliente):
    """Nothing but `executar_no` writes `iniciado_em`: the filled field proves the node was marked
    BEFORE running, which is what backs the UI's "running"."""
    uid, h = await usuario_logado(
        owner_engine, cliente, email="andamento@example.com", perfil="especialista"
    )
    pid = await _pipeline_simples(cliente, h)
    det = await engine.criar_execucao(pipeline_id=uuid.UUID(pid), ator_id=uid)
    for nid in det["ordem"]:
        await engine.executar_no(
            execucao_id=uuid.UUID(det["execucao_id"]),
            definicao=det["definicao"],
            no_id=nid,
            parametros=det["parametros"],
        )

    async with owner_engine.connect() as conn:
        linhas = (
            await conn.execute(
                text(
                    "SELECT no_id, status::text, iniciado_em, concluido_em FROM execucao_nos "
                    "WHERE execucao_id = :e ORDER BY no_id"
                ),
                {"e": uuid.UUID(det["execucao_id"])},
            )
        ).all()
    assert [r[0] for r in linhas] == ["fim", "src"]
    for _no, status, iniciado, concluido in linhas:
        assert status == "concluida"
        assert iniciado is not None, "node completed without recording when it started"
        assert iniciado <= concluido


async def test_marca_de_inicio_nao_reescreve_no_ja_concluido(owner_engine):
    """Reuse guard: re-delivery of an activity finds the node `concluida` and returns without
    repeating the effect. If the start mark overwrote that row, an already committed external POST
    would be fired again."""
    eid, pid = uuid.uuid4(), uuid.uuid4()
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO pipelines (id, nome, versao_publicada_id) VALUES (:p, :n, NULL)"),
            {"p": pid, "n": f"andamento-{pid}"},
        )
        await conn.execute(
            text(
                "INSERT INTO execucoes (id, pipeline_id, versao_numero, status, iniciada_em) "
                "VALUES (:e, :p, 1, 'executando', now())"
            ),
            {"e": eid, "p": pid},
        )
        await conn.execute(
            text(
                "INSERT INTO execucao_nos (id, execucao_id, no_id, status, linhas, hash, snapshot, "
                "duracao_ms, concluido_em) "
                "VALUES (:i, :e, 'efeito', 'concluida', 3, 'h1', '/tmp/x.parquet', 7, now())"
            ),
            {"i": uuid.uuid4(), "e": eid},
        )

    await engine._marcar_no_iniciado(eid, "efeito")

    async with owner_engine.connect() as conn:
        r = (
            await conn.execute(
                text(
                    "SELECT status::text, linhas, hash FROM execucao_nos "
                    "WHERE execucao_id = :e AND no_id = 'efeito'"
                ),
                {"e": eid},
            )
        ).first()
    assert r[0] == "concluida"
    assert (r[1], r[2]) == (3, "h1"), "the start mark erased the completed node's evidence"


async def test_consultar_expoe_o_no_em_andamento(owner_engine, app_engine):
    """The detail must return `status` + `iniciado_em` of the node running NOW: with these two
    fields the UI says which node it is and for how long it has been at it."""
    eid, pid = uuid.uuid4(), uuid.uuid4()
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO pipelines (id, nome, versao_publicada_id) VALUES (:p, :n, NULL)"),
            {"p": pid, "n": f"andamento2-{pid}"},
        )
        await conn.execute(
            text(
                "INSERT INTO execucoes (id, pipeline_id, versao_numero, status, iniciada_em) "
                "VALUES (:e, :p, 1, 'executando', now())"
            ),
            {"e": eid, "p": pid},
        )
        # One completed node and one in progress: the ordering must leave the running one last
        # (it has no `concluido_em`), and not drop it.
        await conn.execute(
            text(
                "INSERT INTO execucao_nos (id, execucao_id, no_id, status, linhas, duracao_ms, "
                "iniciado_em, concluido_em) "
                "VALUES (:i, :e, 'src', 'concluida', 2, 5, now(), now())"
            ),
            {"i": uuid.uuid4(), "e": eid},
        )
        await conn.execute(
            text(
                "INSERT INTO execucao_nos (id, execucao_id, no_id, status, iniciado_em) "
                "VALUES (:i, :e, 'http', 'executando', now())"
            ),
            {"i": uuid.uuid4(), "e": eid},
        )

    async with app_engine.connect() as conn:
        det = await engine.consultar(conn, eid)

    nos = {n["no_id"]: n for n in det["nos"]}
    assert [n["no_id"] for n in det["nos"]] == ["src", "http"]
    assert nos["http"]["status"] == "executando"
    assert nos["http"]["iniciado_em"] is not None
    assert nos["http"]["duracao_ms"] is None, "a node in progress has no final duration"
