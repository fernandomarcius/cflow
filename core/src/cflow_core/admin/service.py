"""Health and usage metrics. Reflects real database data, not mocks."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..timezone import agora_utc, para_exibicao


async def _um(conn: AsyncConnection, sql: str) -> int:
    return int((await conn.execute(text(sql))).scalar_one() or 0)


async def _grupo(conn: AsyncConnection, sql: str) -> dict:
    rows = (await conn.execute(text(sql))).all()
    return {str(k): int(v) for k, v in rows}


def _momento(valor: datetime | None) -> str | None:
    """Timestamp at the edge in the display timezone, with an explicit offset."""
    return para_exibicao(valor).isoformat() if valor is not None else None


async def _linha(conn: AsyncConnection, sql: str) -> dict:
    r = (await conn.execute(text(sql))).mappings().first()
    return {
        k: (float(v) if v is not None and k != "total" else v) for k, v in dict(r or {}).items()
    }


async def metricas(conn: AsyncConnection) -> dict:
    """Real counts for the admin panel. The SELECT itself proves database health."""
    return {
        "banco": "ok",
        "usuarios": {
            "total": await _um(conn, "SELECT count(*) FROM usuarios"),
            "ativos": await _um(conn, "SELECT count(*) FROM usuarios WHERE ativo"),
            "com_mfa": await _um(conn, "SELECT count(*) FROM usuarios WHERE mfa_habilitado"),
            "por_perfil": await _grupo(
                conn, "SELECT perfil::text, count(*) FROM usuarios GROUP BY perfil"
            ),
        },
        "conexoes": {
            "total": await _um(conn, "SELECT count(*) FROM conexoes"),
            "por_tipo": await _grupo(
                conn, "SELECT tipo::text, count(*) FROM conexoes GROUP BY tipo"
            ),
        },
        "pipelines": {
            "total": await _um(conn, "SELECT count(*) FROM pipelines"),
            "publicados": await _um(
                conn, "SELECT count(*) FROM pipelines WHERE versao_publicada_id IS NOT NULL"
            ),
        },
        # Concurrency and queue: tells whether replicas are lacking and whether the guard is acting.
        # Per-node p50/p95 over the last 7 days come from `execucao_nos.duracao_ms`.
        "execucoes": {
            "em_voo": await _um(conn, "SELECT count(*) FROM execucoes WHERE status = 'executando'"),
            "recusadas_24h": await _um(
                conn,
                "SELECT count(*) FROM auditoria WHERE recurso_tipo = 'execucao' "
                "AND depois->>'recusado' = 'execucao_em_andamento' "
                "AND ocorrido_em > now() - interval '24 hours'",
            ),
            "nos_7d": await _linha(
                conn,
                "SELECT count(*) AS total, "
                "  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY duracao_ms)) AS p50_ms, "
                "  round(percentile_cont(0.95) WITHIN GROUP (ORDER BY duracao_ms)) AS p95_ms "
                "FROM execucao_nos WHERE duracao_ms IS NOT NULL "
                "AND concluido_em > now() - interval '7 days'",
            ),
        },
        "sessoes_ativas": await _um(
            conn, "SELECT count(*) FROM sessoes WHERE revogada_em IS NULL AND expira_em > now()"
        ),
        "auditoria": {
            "total": await _um(conn, "SELECT count(*) FROM auditoria"),
            "ultimas_24h": await _um(
                conn,
                "SELECT count(*) FROM auditoria WHERE ocorrido_em > now() - interval '24 hours'",
            ),
        },
        "lake": await metricas_lake(conn),
    }


# --- Lake / external ingestion -----------------------------------------------------------------

# Tolerance allowed BEYOND the cadence interval before a source is declared late.
#
# `INTERVALO` (in `ingestao/service.py`) remains the ONLY source of each cadence's interval -
# none is repeated here: this table holds a different quantity, the tolerance. It is not
# proportional to the interval and so cannot come from a single fraction: 2 h over 24 h is 8 %,
# while 10 days over 30 is 33 %. The daily cadence publishes the same day (2 h covers upstream
# delay); the monthly one runs into the 31-day month PLUS the staggering of the first days of the
# month when bronze pipelines run, and an 8 % margin there would flag lateness at every month turn.
#
# A cadence missing from this table falls back to a default fraction of its own interval: a new
# cadence never starts without an SLA - it starts with a generous one, which is the safe side
# (a false alarm silences the alert).
FOLGA_FRESCOR = {
    "diaria": timedelta(hours=2),
    "semanal": timedelta(days=1),
    "mensal": timedelta(days=10),
    "anual": timedelta(days=35),
}
FRACAO_FOLGA_PADRAO = 0.15


def sla_frescor_horas(cadencia: str | None, contrato: dict | None = None) -> float | None:
    """Source freshness SLA, in hours: cadence interval + tolerance. `None` = no SLA.

    `manual` has no derived SLA - no periodicity is promised, and inventing one would make every
    manual source start out red. **This matters in practice**: when sources are moved to `manual`
    (scheduling is done by the bronze pipeline), the SLO would otherwise be empty. The override is
    the way out: the contract is data, and declaring `frescor.sla_horas` on a manual source gives
    it an SLA without reviving the cadence.
    """
    # `contrato` arrives from jsonb as a dict; the guard exists because this block is also called
    # with whatever comes from the API, and a malformed contract must not take the whole panel down.
    bloco = contrato.get("frescor") if isinstance(contrato, dict) else None
    override = bloco.get("sla_horas") if isinstance(bloco, dict) else None
    if isinstance(override, (int, float)) and not isinstance(override, bool) and override > 0:
        return round(float(override), 1)

    from ..ingestao.service import INTERVALO

    intervalo = INTERVALO.get(cadencia or "manual")
    if intervalo is None:
        return None
    folga = FOLGA_FRESCOR.get(cadencia or "", intervalo * FRACAO_FOLGA_PADRAO)
    return round((intervalo + folga).total_seconds() / 3600, 1)


async def frescor_das_fontes(conn: AsyncConnection) -> list[dict]:
    """Freshness SLO PER SOURCE - the metric no infrastructure alert can produce.

    An infrastructure alert sees "the whole ingestion died"; this one sees "one feed stopped and
    the rest did not", which is the really likely outcome (the upstream changes a URL while the
    other datasets keep going).

    A single query for all sources: `repositorio.listar_fontes` already brings the watermark and the
    last run via an indexed subselect (`idx_ingestao_execucoes_fonte`). Source counts come from the
    SAME rows, in Python - two separate computations would diverge the day one of them changed.

    **The freshness reference is the watermark, not the run.** A run that executed and brought
    nothing (404 all month, `304` on everything) is exactly the case to catch: measuring by
    `ultima_execucao_em` would show the source green while the data ages. The run is used as the
    reference only when there is no watermark - a source that never closed a slice.

    **An inactive source has no SLA.** Deactivating is how a reload is paused (inactive sources are
    treated as a warning, not an error); keeping it red forever is guaranteed noise, and permanent
    noise is what makes nobody look at the panel.
    """
    from ..ingestao import repositorio as repo

    fontes = await repo.listar_fontes(conn)
    agora = agora_utc()
    linhas: list[dict] = []
    for f in fontes:
        contrato = f.get("contrato") or {}
        sla = sla_frescor_horas(f.get("cadencia"), contrato)
        referencia_em = f.get("watermark_em") or f.get("ultima_execucao_em")
        referencia = (
            "watermark"
            if f.get("watermark_em")
            else ("execucao" if f.get("ultima_execucao_em") else None)
        )
        atraso = None
        if referencia_em is not None:
            if referencia_em.tzinfo is None:
                referencia_em = referencia_em.replace(tzinfo=agora.tzinfo)
            atraso = round((agora - referencia_em).total_seconds() / 3600, 1)

        if sla is None or not f.get("ativo"):
            dentro = None  # no applicable SLA - neither "ok" nor "late"
        elif atraso is None:
            dentro = False  # has an SLA and never closed any slice: not fresh
        else:
            dentro = atraso <= sla

        linhas.append(
            {
                "fonte_id": str(f["id"]),
                "nome": f["nome"],
                "dataset": f["dataset"],
                "cadencia": f.get("cadencia"),
                "ativa": bool(f.get("ativo")),
                "watermark": f.get("watermark"),
                "watermark_em": _momento(f.get("watermark_em")),
                "ultima_execucao_em": _momento(f.get("ultima_execucao_em")),
                "ultimo_status": f.get("ultimo_status"),
                "referencia": referencia,
                "atraso_horas": atraso,
                "sla_horas": sla,
                "dentro_do_sla": dentro,
            }
        )

    # Worst first: those outside the SLA, from most to least late. A list ordered by name would force
    # scanning every row to find the one that matters.
    linhas.sort(
        key=lambda ln: (
            0 if ln["dentro_do_sla"] is False else 1,
            -(ln["atraso_horas"] or 0),
            ln["nome"],
        )
    )
    return linhas


async def metricas_lake(conn: AsyncConnection) -> dict:
    """The lake in the admin panel: sources, runs, batches, quarantine and the freshness SLO.

    The panel advertises "health and usage metrics"; without this block the half of the platform
    that talks to the outside world would have no numbers at all.

    Cost: 4 aggregate queries + `listar_fontes` + `fatias_sem_lote_bom`. The three scans
    (`ingestao_execucoes` by window, all of `ingestao_lotes`, all of `ingestao_quarentena`) have no
    serving index and grow with history - they are cheap at typical sizes; if it ever hurts, the
    way out is to materialize per run, not to hide the number.
    """
    from ..ingestao import repositorio as repo

    fontes = await frescor_das_fontes(conn)
    por_cadencia: dict[str, int] = {}
    for ln in fontes:
        chave = ln["cadencia"] or "manual"
        por_cadencia[chave] = por_cadencia.get(chave, 0) + 1

    lotes = (
        (
            await conn.execute(
                text(
                    "SELECT count(*) AS total, "
                    "count(*) FILTER (WHERE status = 'bronze') AS bronze, "
                    "coalesce(sum(bytes), 0) AS bytes, coalesce(sum(linhas), 0) AS linhas "
                    "FROM ingestao_lotes"
                )
            )
        )
        .mappings()
        .one()
    )
    quarentena = (
        (
            await conn.execute(
                text(
                    "SELECT count(*) AS total, "
                    "count(*) FILTER (WHERE criado_em > now() - interval '24 hours') AS ultimas_24h, "
                    "count(DISTINCT fonte_id) AS fontes_afetadas FROM ingestao_quarentena"
                )
            )
        )
        .mappings()
        .one()
    )
    # Reuses the quarantine tracking query: a slice that failed and NEVER landed in bronze. It is the
    # only number here that says "data is missing" rather than "an error happened" - neither replaces the other.
    buracos = await repo.fatias_sem_lote_bom(conn)

    return {
        "fontes": {
            "total": len(fontes),
            "ativas": sum(1 for ln in fontes if ln["ativa"]),
            "por_cadencia": por_cadencia,
        },
        "execucoes_24h": await _grupo(
            conn,
            "SELECT status, count(*) FROM ingestao_execucoes "
            "WHERE iniciada_em > now() - interval '24 hours' GROUP BY status",
        ),
        "lotes": {
            "total": int(lotes["total"] or 0),
            "bronze": int(lotes["bronze"] or 0),
            "bytes": int(lotes["bytes"] or 0),
            "linhas": int(lotes["linhas"] or 0),
        },
        "quarentena": {
            "total": int(quarentena["total"] or 0),
            "ultimas_24h": int(quarentena["ultimas_24h"] or 0),
            "fontes_afetadas": int(quarentena["fontes_afetadas"] or 0),
            "fatias_sem_lote_bom": len(buracos),
        },
        "frescor": {
            "fora_do_sla": sum(1 for ln in fontes if ln["dentro_do_sla"] is False),
            "sem_sla": sum(1 for ln in fontes if ln["dentro_do_sla"] is None),
            "fontes": fontes,
        },
    }


async def exportar(conn: AsyncConnection, pipeline_ids: list | None = None) -> dict:
    """Promotion bundle between environments (v2): PUBLISHED version, dependency order,
    classification and connections WITHOUT secrets - see `admin.promocao`."""
    from . import promocao

    return await promocao.exportar(conn, pipeline_ids)


def _remap_conexoes(definicao: dict, mapa: dict) -> dict:
    """Re-point conexao_id of source nodes to the target environment's ids."""
    import copy

    d = copy.deepcopy(definicao)
    for no in d.get("nos", []):
        cfg = no.get("config") or {}
        cid = cfg.get("conexao_id")
        if cid and str(cid) in mapa:
            cfg["conexao_id"] = mapa[str(cid)]
    return d


async def importar(
    conn: AsyncConnection,
    bundle: dict,
    *,
    ator_id,
    ip=None,
    request_id=None,
    atualizar: bool = False,
    simular: bool = False,
) -> dict:
    """Import a bundle (v1 or v2) - see `admin.promocao`: matches by name, creates in dependency
    order re-pointing connection and pipeline, only publishes a new version of an existing item with
    `atualizar`; `simular` returns the plan without writing."""
    from . import promocao

    return await promocao.importar(
        conn,
        bundle,
        atualizar=atualizar,
        simular=simular,
        ator_id=ator_id,
        ip=ip,
        request_id=request_id,
    )


async def saude_conexoes() -> list[dict]:
    """Test every registered connection and report errors. Sequential; sanitized cause.

    The list is read in a short transaction that CLOSES before the first test: testing N
    connections is N network round trips against third-party systems, and keeping the metadata
    transaction open that long blocks autovacuum for the whole database.
    """
    from ..conexoes import service as cs
    from ..db import transacao

    async with transacao() as conn:
        rows = (
            (
                await conn.execute(
                    text("SELECT id, nome, tipo::text AS tipo FROM conexoes ORDER BY nome")
                )
            )
            .mappings()
            .all()
        )
        conexoes_cadastradas = [(r["id"], r["nome"], r["tipo"]) for r in rows]

    saude = []
    for cid, nome, tipo in conexoes_cadastradas:
        res = await cs.testar(cid)
        saude.append({"nome": nome, "tipo": tipo, "ok": res["ok"], "causa": res.get("causa")})
    return saude
