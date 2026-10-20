"""Single-statement state writes in autocommit.

`db.autocommit()` drops BEGIN/COMMIT (two event-loop round trips) from the `execucao_nos` state
writes that are a single statement (mark, reuse, completion without trail, failure). The contract:
each statement persists on its own, and nothing that writes audit goes through here.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from cflow_core import db
from cflow_core.execucao import engine


async def _execucao(owner_engine) -> uuid.UUID:
    eid, pid = uuid.uuid4(), uuid.uuid4()
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO pipelines (id, nome, versao_publicada_id) VALUES (:p, :n, NULL)"),
            {"p": pid, "n": f"autocommit-{pid}"},
        )
        await conn.execute(
            text(
                "INSERT INTO execucoes (id, pipeline_id, versao_numero, status, iniciada_em) "
                "VALUES (:e, :p, 1, 'executando', now())"
            ),
            {"e": eid, "p": pid},
        )
    return eid


async def _status(owner_engine, eid) -> dict:
    async with owner_engine.connect() as conn:
        linhas = (
            await conn.execute(
                text(
                    "SELECT no_id, status::text, linhas, hash FROM execucao_nos "
                    "WHERE execucao_id = :e"
                ),
                {"e": eid},
            )
        ).all()
    return {r[0]: (r[1], r[2], r[3]) for r in linhas}


async def test_autocommit_persiste_o_statement_mesmo_com_erro_depois(owner_engine):
    eid = await _execucao(owner_engine)
    with pytest.raises(RuntimeError):
        async with db.autocommit() as conn:
            await conn.execute(engine._SQL_MARCAR_INICIADO, {"e": eid, "n": "no1"})
            raise RuntimeError("error after the write")
    # In `transacao()` the error would undo the mark; in autocommit it was its own transaction.
    assert (await _status(owner_engine, eid))["no1"][0] == "executando"


async def test_marca_em_autocommit_poupa_o_concluido_e_reabre_a_falha(owner_engine):
    eid = await _execucao(owner_engine)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO execucao_nos (id, execucao_id, no_id, status, linhas, hash, snapshot, "
                "duracao_ms, concluido_em) "
                "VALUES (:i, :e, 'feito', 'concluida', 3, 'h1', '/tmp/x.parquet', 7, now())"
            ),
            {"i": uuid.uuid4(), "e": eid},
        )
        await conn.execute(
            text(
                "INSERT INTO execucao_nos (id, execucao_id, no_id, status, erro, concluido_em) "
                "VALUES (:i, :e, 'quebrou', 'falha', 'x', now())"
            ),
            {"i": uuid.uuid4(), "e": eid},
        )
    for no in ("feito", "quebrou", "novo"):
        await engine._marcar_no_iniciado(eid, no)
    st = await _status(owner_engine, eid)
    assert st["feito"] == ("concluida", 3, "h1")  # the completed node's evidence is kept
    assert st["quebrou"][0] == "executando"  # retry: the mark reopens the failure, as before
    assert st["novo"][0] == "executando"
