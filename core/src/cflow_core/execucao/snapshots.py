"""Resolution of the Parquet snapshot of a pipeline node (stored in `execucao_nos`).

Shared by cube/view/page consumption (`modelagem.consulta`) and the **`pipeline` source node**
(engine/preview). References the EXACT run (by `execucao_id`) or the latest completed run of a
version/pipeline (never an arbitrary "latest" without criteria).
"""

from __future__ import annotations

import json
import os
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import ErroAplicacao

_BASE = (
    "SELECT en.snapshot, en.hash, en.execucao_id, e.parametros FROM execucao_nos en "
    "JOIN execucoes e ON e.id = en.execucao_id "
    "WHERE e.pipeline_id = :p AND en.no_id = :n "
    "AND en.status = 'concluida' AND e.status = 'concluida'"
)


async def resolver_snapshot(
    conn: AsyncConnection,
    pipeline_id: uuid.UUID | str,
    no_id: str,
    *,
    execucao_id: uuid.UUID | str | None = None,
    versao_numero: int | None = None,
    parametros: dict | None = None,
) -> dict:
    """Snapshot + origin (for the trace chain). Priority: exact `execucao_id` -> latest completed
    run of `versao_numero` -> latest completed run of the pipeline.

    `parametros` RESTRICTS the "latest completed" to runs that used those values, already
    normalized and redacted as `execucoes.parametros` stores them. Without it, "the latest" is the
    latest of ANY as-of date, so a report for one period could silently read the calculation of
    another: a plausible and wrong result, the worst outcome.

    Errors: `sem_execucao` (no completed run; the message NAMES the requested cut when there was
    a filter) and `snapshot_ausente` (file not found on the server).
    """
    params: dict = {"p": uuid.UUID(str(pipeline_id)), "n": no_id}
    filtro = ""
    if parametros:
        # `@>` (contains) rather than equality: the source pipeline declares a SUBSET of the root
        # parameters, and the caller already narrows to what it declares. Equality would require
        # reproducing the parameter expansion here.
        filtro = " AND e.parametros @> CAST(:params_json AS jsonb)"
        params["params_json"] = json.dumps(parametros, sort_keys=True, default=str)
    if execucao_id is not None:
        sql = _BASE + " AND e.id = :ex"
        params["ex"] = uuid.UUID(str(execucao_id))
    elif versao_numero is not None:
        sql = _BASE + filtro + " AND e.versao_numero = :v ORDER BY e.concluida_em DESC LIMIT 1"
        params["v"] = versao_numero
    else:
        sql = _BASE + filtro + " ORDER BY e.concluida_em DESC LIMIT 1"

    r = (await conn.execute(text(sql), params)).mappings().first()
    if not r:
        # Naming the cut separates "has not run yet" from "ran with another as-of". Serving the
        # latest of another cut would produce a plausible but wrong result.
        detalhe = f" com {parametros}" if parametros else ""
        raise ErroAplicacao(
            codigo="sem_execucao",
            mensagem=f"The source pipeline has no completed run{detalhe}. "
            "Run the source pipeline first.",
            status=400,
        )
    if not os.path.isfile(r["snapshot"]):
        raise ErroAplicacao(
            codigo="snapshot_ausente",
            mensagem="Source run snapshot not found on the server.",
            status=400,
        )
    return {
        "snapshot": r["snapshot"],
        "execucao_id": str(r["execucao_id"]),
        "no_id": no_id,
        "hash": r["hash"],
    }
