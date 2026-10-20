"""Execution scheduling: friendly recurrence + validity window + target (pipeline x page) +
parameters (with relative periods). The worker fires the due schedules periodically.

The DISPATCH is injected (inline in tests; via Temporal in the worker), so the core is not coupled
to orchestration. `dispatch(pipeline_id, criado_por, parametros, pagina_id, agendamento_id) -> str |
None` creates the execution (origin='agendado') and returns the execucao_id (or None).

Recurrence (`agenda`, jsonb): {tipo: horaria|diaria|semanal|mensal, a_cada_horas?, horario:"HH:MM",
dias_semana?:[0..6] (0=Sunday), dia_mes?:1..31}. Times are in America/Sao_Paulo. `vigencia_inicio`/
`vigencia_fim` (optional dates) bound when the schedule is active. Parameter dates can be relative
TOKENS via `parametros["@periodo"]` (resolved at fire time)."""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..audit import Acao, AtorTipo, registrar
from ..conexoes import service as conexoes
from ..db import transacao
from ..errors import DetalheErro, ErroAplicacao
from ..periodos import resolver_janela
from ..timezone import FUSO_SAO_PAULO, agora_utc, para_exibicao

# dispatch takes (pipeline_id, criado_por, parametros, pagina_id, agendamento_id) and returns the exec id.
Dispatch = Callable[
    [uuid.UUID, uuid.UUID, dict, uuid.UUID | None, uuid.UUID], Awaitable[str | None]
]

_log = logging.getLogger("cflow_core.agendador")

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_TIPOS_AGENDA = ("horaria", "diaria", "semanal", "mensal")


# ---------------------------------------------------------------------------
# Recurrence -> next firing (UTC); times are in America/Sao_Paulo.
# ---------------------------------------------------------------------------
def _validar_agenda(agenda: dict) -> None:
    det: list[DetalheErro] = []
    tipo = (agenda or {}).get("tipo")
    if tipo not in _TIPOS_AGENDA:
        raise ErroAplicacao(
            codigo="agenda_invalida",
            mensagem="Invalid schedule.",
            status=400,
            detalhes=[
                DetalheErro(campo="agenda", mensagem=f"tipo must be one of {list(_TIPOS_AGENDA)}.")
            ],
        )
    if tipo == "horaria":
        n = agenda.get("a_cada_horas")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            det.append(
                DetalheErro(campo="agenda", mensagem="a_cada_horas must be an integer >= 1.")
            )
    else:
        if not _HHMM.match(str(agenda.get("horario") or "")):
            det.append(DetalheErro(campo="agenda", mensagem="horario must be 'HH:MM'."))
        if tipo == "semanal":
            dias = agenda.get("dias_semana")
            if (
                not isinstance(dias, list)
                or not dias
                or any(
                    not isinstance(d, int) or isinstance(d, bool) or d < 0 or d > 6 for d in dias
                )
            ):
                det.append(
                    DetalheErro(
                        campo="agenda", mensagem="dias_semana must list days 0..6 (0=Sunday)."
                    )
                )
        if tipo == "mensal":
            dm = agenda.get("dia_mes")
            if not isinstance(dm, int) or isinstance(dm, bool) or dm < 1 or dm > 31:
                det.append(DetalheErro(campo="agenda", mensagem="dia_mes must be 1..31."))
    if det:
        raise ErroAplicacao(
            codigo="agenda_invalida", mensagem="Invalid schedule.", status=400, detalhes=det
        )


def _hhmm(horario) -> time:
    h, m = str(horario or "00:00").split(":")[:2]
    return time(int(h), int(m))


def _dow_br(dt: datetime) -> int:
    """Day of week 0=Sunday..6=Saturday. `weekday()` is Monday=0..Sunday=6."""
    return (dt.weekday() + 1) % 7


def _ultimo_dia_mes(ano: int, mes: int) -> int:
    prox = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)
    return (prox - timedelta(days=1)).day


def proxima_em(
    agenda: dict, *, vigencia_inicio: date | None = None, depois: datetime | None = None
) -> datetime:
    """Next firing in UTC after `depois` (UTC; defaults to now). Honors `vigencia_inicio` (never
    fires before the validity window starts). Times are in America/Sao_Paulo."""
    base_utc = depois or agora_utc()
    agora_sp = para_exibicao(base_utc, FUSO_SAO_PAULO)
    tipo = (agenda or {}).get("tipo")

    if tipo == "horaria":
        n = max(1, int(agenda.get("a_cada_horas") or 1))
        prox_sp = agora_sp + timedelta(hours=n)
    elif tipo == "diaria":
        hhmm = _hhmm(agenda.get("horario"))
        cand = agora_sp.replace(hour=hhmm.hour, minute=hhmm.minute, second=0, microsecond=0)
        prox_sp = cand if cand > agora_sp else cand + timedelta(days=1)
    elif tipo == "semanal":
        dias = sorted({int(d) for d in (agenda.get("dias_semana") or [])}) or [_dow_br(agora_sp)]
        hhmm = _hhmm(agenda.get("horario"))
        prox_sp = agora_sp + timedelta(days=1)  # fallback
        for delta in range(0, 8):
            d = agora_sp + timedelta(days=delta)
            if _dow_br(d) in dias:
                cand = d.replace(hour=hhmm.hour, minute=hhmm.minute, second=0, microsecond=0)
                if cand > agora_sp:
                    prox_sp = cand
                    break
    elif tipo == "mensal":
        hhmm = _hhmm(agenda.get("horario"))
        dia = max(1, int(agenda.get("dia_mes") or 1))

        def _cand(ano: int, mes: int) -> datetime:
            d = min(dia, _ultimo_dia_mes(ano, mes))
            return agora_sp.replace(
                year=ano,
                month=mes,
                day=d,
                hour=hhmm.hour,
                minute=hhmm.minute,
                second=0,
                microsecond=0,
            )

        cand = _cand(agora_sp.year, agora_sp.month)
        if cand <= agora_sp:
            ano, mes = (
                (agora_sp.year + 1, 1)
                if agora_sp.month == 12
                else (agora_sp.year, agora_sp.month + 1)
            )
            cand = _cand(ano, mes)
        prox_sp = cand
    else:
        prox_sp = agora_sp + timedelta(
            days=1
        )  # unrecognized schedule -> daily (backward compatible)

    if vigencia_inicio is not None:
        inicio_sp = datetime.combine(vigencia_inicio, time(0, 0), tzinfo=FUSO_SAO_PAULO)
        if prox_sp < inicio_sp:
            # recompute from the start of the validity window (without the guard, to avoid recursing)
            return proxima_em(agenda, vigencia_inicio=None, depois=inicio_sp.astimezone(UTC))
    return prox_sp.astimezone(UTC)


#: Value that, in a schedule parameter, becomes the INSTANT of the firing.
#:
#: A schedule stores the PARAMETER, not the instant: `as_of: "today"` would resolve to `today
#: 23:59:59`, a cutoff in the FUTURE for an overnight occurrence that claims to have seen the whole
#: day. Anyone reusing that load at 15:00 inherits the claim. The as-of must be an instant that
#: has actually passed.
VALOR_AGORA = "@agora"

#: America/Sao_Paulo without depending on tzdata: it is the zone the scheduler already reasons in
#: (`proxima_em`). Brazil has had no DST since 2019; if it returns, this becomes a silent one-hour
#: error and should be replaced with ZoneInfo.
_FUSO_SP = timezone(timedelta(hours=-3))


def _aplicar_agora(parametros: dict, agora: datetime | None = None) -> dict:
    """Replaces every `"@agora"` value with the firing instant, at one-second precision.

    ONE instant for the whole occurrence, not one per operation: the fan-out calls this BEFORE
    expanding, so all operations of a night share the stamp. One instant per operation would mean
    many distinct snapshots per night, and "today's load" would stop having a single answer.

    Second precision because it is what `resolver_corte` returns and what SQL `datetime` compares
    without surprises.
    """
    carimbo = (agora or datetime.now(_FUSO_SP)).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    return {k: (carimbo if v == VALOR_AGORA else v) for k, v in parametros.items()}


def _aplicar_periodos(parametros: dict | None) -> dict:
    """Resolves `parametros["@periodo"]` (relative token) into CONCRETE start/end dates at firing
    time (same resolution as the portal, but in the backend). Each entry: {token, param_inicio,
    param_fim}. Accepts one entry or a list. Removes '@periodo' from the result."""
    p = dict(parametros or {})
    spec = p.pop("@periodo", None)
    if not spec:
        return p
    for it in spec if isinstance(spec, list) else [spec]:
        if not isinstance(it, dict):
            continue
        try:
            ini, fim = resolver_janela(str(it.get("token")))
        except ValueError:
            continue
        if it.get("param_inicio"):
            p[it["param_inicio"]] = ini
        if it.get("param_fim"):
            p[it["param_fim"]] = fim
    return p


# ---------------------------------------------------------------------------
# CRUD (inside the route's transaction; audited).
# ---------------------------------------------------------------------------
async def _recusar_segredo_no_agendamento(
    conn: AsyncConnection, pipeline_id: uuid.UUID, parametros: dict | None
) -> None:
    """A schedule runs on its own, so it would have to store the parameter value in clear text.
    A `segredo: true` parameter cannot be stored, so scheduling with one is refused instead of
    saving a credential or failing silently at firing time (redacted value).

    Looks at the WHOLE CHAIN, not just the scheduled pipeline: since firing is chained, the schedule
    parameters also reach the upstreams, and a `segredo: true` declared only further up would slip
    past the check and be stored in clear text in `agendamentos.parametros`. If the closure does not
    resolve (pipeline without a published version, cycle), it falls back to the pipeline's own
    definition: refusing the schedule because of an upstream's state would trade a leak for a
    blockage.

    This guard belongs to the CRUD and is evaluated ONCE, on create/edit: it does NOT cover a chain
    that changes later (someone publishes an upstream version that declares the credential, or adds
    a pipeline-type source). That gap is closed by `execucao.disparo._recusar_segredo_guardado`,
    evaluated on every FIRING; the refusal here makes the error show up to whoever schedules, not
    at 01:00.
    """
    if not parametros:
        return
    from ..pipelines import service as ps
    from ..pipelines.dag import resolver_ordem
    from ..pipelines.definicao import nomes_segredo

    det = await ps.obter(conn, pipeline_id, None)
    definicoes = [det.get("definicao") or {}]
    if det["numero"]:
        try:
            definicoes = [i["definicao"] for i in await resolver_ordem(conn, pipeline_id, None)]
        except ErroAplicacao:
            pass
    informados = set().union(*(nomes_segredo(d) for d in definicoes)) & set(parametros)
    if informados:
        raise ErroAplicacao(
            codigo="agendamento_com_segredo",
            mensagem=(
                "Cannot schedule with a credential parameter "
                f"({', '.join(sorted(informados))}): the schedule would store the value. "
                "Keep the credential in the connection vault."
            ),
            status=400,
        )


async def criar(
    conn: AsyncConnection,
    *,
    pipeline_id: uuid.UUID,
    agenda: dict,
    nome: str | None = None,
    pagina_id: uuid.UUID | None = None,
    parametros: dict | None = None,
    vigencia_inicio: date | None = None,
    vigencia_fim: date | None = None,
    criado_por: uuid.UUID,
    ip=None,
    request_id=None,
) -> dict:
    _validar_agenda(agenda)
    await _recusar_segredo_no_agendamento(conn, pipeline_id, parametros)
    aid = uuid.uuid4()
    prox = proxima_em(agenda, vigencia_inicio=vigencia_inicio)
    await conn.execute(
        text(
            "INSERT INTO agendamentos (id, pipeline_id, nome, pagina_id, parametros, agenda, "
            "  vigencia_inicio, vigencia_fim, proxima_em, criado_por) "
            "VALUES (:id, :p, :nome, :pag, CAST(:par AS jsonb), CAST(:ag AS jsonb), "
            "  :vi, :vf, :px, :c)"
        ),
        {
            "id": aid,
            "p": pipeline_id,
            "nome": nome,
            "pag": pagina_id,
            "par": _json(parametros or {}),
            "ag": _json(agenda),
            "vi": vigencia_inicio,
            "vf": vigencia_fim,
            "px": prox,
            "c": criado_por,
        },
    )
    await registrar(
        conn,
        acao=Acao.CRIAR,
        recurso_tipo="agendamento",
        recurso_id=aid,
        ator_id=criado_por,
        antes=None,
        depois={
            "pipeline_id": str(pipeline_id),
            "nome": nome,
            "pagina_id": str(pagina_id) if pagina_id else None,
            "parametros": parametros or {},
            "agenda": agenda,
            "vigencia_inicio": str(vigencia_inicio) if vigencia_inicio else None,
            "vigencia_fim": str(vigencia_fim) if vigencia_fim else None,
        },
        request_id=request_id,
        ip=ip,
    )
    return {"id": str(aid), "proxima_em": prox.isoformat()}


async def editar(
    conn: AsyncConnection,
    agendamento_id: uuid.UUID,
    *,
    agenda: dict,
    nome: str | None = None,
    pagina_id: uuid.UUID | None = None,
    parametros: dict | None = None,
    vigencia_inicio: date | None = None,
    vigencia_fim: date | None = None,
    ator_id: uuid.UUID,
    ip=None,
    request_id=None,
) -> None:
    _validar_agenda(agenda)
    antes = (
        (
            await conn.execute(
                text(
                    "SELECT pipeline_id, nome, agenda, parametros FROM agendamentos WHERE id = :id"
                ),
                {"id": agendamento_id},
            )
        )
        .mappings()
        .first()
    )
    if antes is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Schedule not found.", status=404)
    await _recusar_segredo_no_agendamento(conn, antes["pipeline_id"], parametros)
    prox = proxima_em(agenda, vigencia_inicio=vigencia_inicio)
    await conn.execute(
        text(
            "UPDATE agendamentos SET nome=:nome, pagina_id=:pag, parametros=CAST(:par AS jsonb), "
            "  agenda=CAST(:ag AS jsonb), vigencia_inicio=:vi, vigencia_fim=:vf, proxima_em=:px "
            "WHERE id=:id"
        ),
        {
            "nome": nome,
            "pag": pagina_id,
            "par": _json(parametros or {}),
            "ag": _json(agenda),
            "vi": vigencia_inicio,
            "vf": vigencia_fim,
            "px": prox,
            "id": agendamento_id,
        },
    )
    await registrar(
        conn,
        acao=Acao.EDITAR,
        recurso_tipo="agendamento",
        recurso_id=agendamento_id,
        ator_id=ator_id,
        antes={"nome": antes["nome"], "agenda": antes["agenda"], "parametros": antes["parametros"]},
        depois={"nome": nome, "agenda": agenda, "parametros": parametros or {}},
        request_id=request_id,
        ip=ip,
    )


async def desativar(
    conn: AsyncConnection,
    agendamento_id: uuid.UUID,
    *,
    ator_id: uuid.UUID,
    ip=None,
    request_id=None,
) -> None:
    await conn.execute(
        text("UPDATE agendamentos SET ativo = false WHERE id = :id"), {"id": agendamento_id}
    )
    await registrar(
        conn,
        acao=Acao.EDITAR,
        recurso_tipo="agendamento",
        recurso_id=agendamento_id,
        ator_id=ator_id,
        antes={"ativo": True},
        depois={"ativo": False},
        request_id=request_id,
        ip=ip,
    )


async def ativar(
    conn: AsyncConnection,
    agendamento_id: uuid.UUID,
    *,
    ator_id: uuid.UUID,
    ip=None,
    request_id=None,
) -> None:
    """Reactivates: recomputes `proxima_em` from now (honoring the validity window)."""
    row = (
        (
            await conn.execute(
                text("SELECT agenda, vigencia_inicio FROM agendamentos WHERE id = :id"),
                {"id": agendamento_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Schedule not found.", status=404)
    prox = proxima_em(row["agenda"] or {}, vigencia_inicio=row["vigencia_inicio"])
    await conn.execute(
        text("UPDATE agendamentos SET ativo = true, proxima_em = :px WHERE id = :id"),
        {"px": prox, "id": agendamento_id},
    )
    await registrar(
        conn,
        acao=Acao.EDITAR,
        recurso_tipo="agendamento",
        recurso_id=agendamento_id,
        ator_id=ator_id,
        antes={"ativo": False},
        depois={"ativo": True},
        request_id=request_id,
        ip=ip,
    )


async def listar(conn: AsyncConnection, pipeline_id: uuid.UUID) -> list[dict]:
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT a.id, a.nome, a.pagina_id, p.nome AS pagina_nome, a.parametros, a.agenda, a.ativo, "
                    "       a.vigencia_inicio, a.vigencia_fim, a.proxima_em, a.ultimo_status, a.ultima_execucao_id, "
                    "       a.ultimo_disparo_em "
                    "FROM agendamentos a LEFT JOIN paginas p ON p.id = a.pagina_id "
                    "WHERE a.pipeline_id = :p ORDER BY a.criado_em"
                ),
                {"p": pipeline_id},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "id": str(r["id"]),
            "nome": r["nome"],
            "pagina_id": str(r["pagina_id"]) if r["pagina_id"] else None,
            "pagina_nome": r["pagina_nome"],
            "parametros": r["parametros"],
            "agenda": r["agenda"],
            "ativo": r["ativo"],
            "vigencia_inicio": r["vigencia_inicio"].isoformat() if r["vigencia_inicio"] else None,
            "vigencia_fim": r["vigencia_fim"].isoformat() if r["vigencia_fim"] else None,
            "proxima_em": r["proxima_em"].isoformat() if r["proxima_em"] else None,
            # WHEN the last firing happened: a bare "failed" does not say whether it is recent or old.
            "ultimo_disparo_em": (
                r["ultimo_disparo_em"].isoformat() if r.get("ultimo_disparo_em") else None
            ),
            "ultimo_status": r["ultimo_status"],
            "ultima_execucao_id": str(r["ultima_execucao_id"]) if r["ultima_execucao_id"] else None,
        }
        for r in rows
    ]


async def listar_todos(conn: AsyncConnection) -> list[dict]:
    """ALL schedules (global list, like the other entities) + pipeline/page name."""
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT a.id, a.nome, a.pipeline_id, pl.nome AS pipeline_nome, a.pagina_id, "
                    "       pg.nome AS pagina_nome, a.agenda, a.ativo, a.proxima_em, a.ultimo_status, "
                    "       a.ultimo_disparo_em "
                    "FROM agendamentos a "
                    "JOIN pipelines pl ON pl.id = a.pipeline_id "
                    "LEFT JOIN paginas pg ON pg.id = a.pagina_id "
                    "ORDER BY a.criado_em DESC"
                )
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "id": str(r["id"]),
            "nome": r["nome"],
            "pipeline_id": str(r["pipeline_id"]),
            "pipeline_nome": r["pipeline_nome"],
            "pagina_id": str(r["pagina_id"]) if r["pagina_id"] else None,
            "pagina_nome": r["pagina_nome"],
            "agenda": r["agenda"],
            "ativo": r["ativo"],
            "proxima_em": r["proxima_em"].isoformat() if r["proxima_em"] else None,
            # WHEN the last firing happened: a bare "failed" does not say whether it is recent or old.
            "ultimo_disparo_em": (
                r["ultimo_disparo_em"].isoformat() if r.get("ultimo_disparo_em") else None
            ),
            "ultimo_status": r["ultimo_status"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Firing (own transactions; called from the worker/tests).
# ---------------------------------------------------------------------------
async def _execucao_pagina(conn: AsyncConnection, pagina_id) -> dict | None:
    """The `execucao` (operator) config of the published page, to learn the operation param/map for the fan-out."""
    row = (
        await conn.execute(
            text(
                "SELECT pv.definicao FROM paginas p JOIN pagina_versoes pv ON pv.id = p.versao_publicada_id "
                "WHERE p.id = :id"
            ),
            {"id": pagina_id},
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    d = json.loads(row) if isinstance(row, str) else row
    return d.get("execucao") if isinstance(d, dict) else None


def _campo(linha: dict, nome: str):
    if nome in linha:
        return linha[nome]
    alvo = str(nome).lower().strip()
    for k, v in linha.items():
        if str(k).lower().strip() == alvo:
            return v
    return None


async def _todas_operacoes(conn: AsyncConnection, opcfg: dict) -> list[str]:
    """DISTINCT operation ids from the operator map (`campo_id` column); used when the selection
    is empty (= all)."""
    mapa, campo = opcfg.get("mapa"), opcfg.get("campo_id")
    if not (mapa and campo):
        return []
    conns = await conexoes.listar(conn)
    cid = next((c["id"] for c in conns if c["nome"] == mapa), None)
    if not cid:
        return []
    try:
        _cols, linhas = await conexoes.amostra(uuid.UUID(str(cid)), "", 5000, None)
    except Exception:  # noqa: BLE001 — unreadable map -> no operations
        return []
    vistos: set[str] = set()
    out: list[str] = []
    for linha in linhas:
        s = str(_campo(linha, campo) or "").strip()
        if s and s not in vistos:
            vistos.add(s)
            out.append(s)
    return out


async def _operacoes_por_consulta(spec: dict) -> list[str]:
    """The operations of a QUERY, not of the page map.

    The map (`_todas_operacoes`) answers "which operations does this page show". Some schedules
    have a different scope, for example a nightly load over operations in a given state of the
    source system rather than a page row. Without this, the only option would be a hard-coded
    list, and a hard-coded list goes stale silently: a new operation goes live and the night simply
    does not load it.

    `{conexao, sql, campo}`. An empty list is an ERROR, not "none": a SQL that stopped matching
    would return zero and the occurrence would pass unnoticed, with no load and no complaint.
    """
    nome, sql, campo = spec.get("conexao"), spec.get("sql"), spec.get("campo")
    if not (nome and sql and campo):
        raise ErroAplicacao(
            codigo="agendamento_invalido",
            mensagem="The operations query requires 'conexao', 'sql' and 'campo'.",
            status=400,
        )
    async with transacao() as c2:
        conns = await conexoes.listar(c2)
    cid = next((x["id"] for x in conns if x["nome"] == nome), None)
    if not cid:
        raise ErroAplicacao(
            codigo="agendamento_invalido",
            mensagem=f"The connection '{nome}' of the operations query does not exist.",
            status=400,
        )
    _cols, linhas = await conexoes.amostra(uuid.UUID(str(cid)), str(sql), 5000, None)
    vistos: set[str] = set()
    out: list[str] = []
    for linha in linhas:
        v = str(_campo(linha, str(campo)) or "").strip()
        if v and v not in vistos:
            vistos.add(v)
            out.append(v)
    if not out:
        raise ErroAplicacao(
            codigo="agendamento_sem_operacoes",
            mensagem="The operations query returned no operations.",
            status=400,
        )
    return out


async def _expandir(conn: AsyncConnection, ag: dict) -> list[dict]:
    """Resolves the period, stamps `@agora` and EXPANDS the multi-select of operations (fan-out).

    `@operacoes` absent -> 1 set; empty list -> ALL operations in the map; list -> one per
    operation; DICT `{conexao, sql, campo}` -> the operations the query returns, for scopes that
    are not a page map (for example a nightly load selected by operation status).

    Each operation becomes one execution (the derived one is resolved PER operation in
    criar_execucao). `@agora` is stamped BEFORE the fan-out on purpose: the whole occurrence shares
    a single as-of.
    """
    base = _aplicar_agora(_aplicar_periodos(ag.get("parametros")))
    if "@operacoes" not in base:
        return [base]
    selecao = base.pop("@operacoes")
    if isinstance(selecao, dict):
        # The parameter that receives the operation comes from the query itself: without a page
        # there is no `opcfg["param"]` to take it from.
        destino = str(selecao.get("param") or "operacao_id")
        return [{**base, destino: i} for i in await _operacoes_por_consulta(selecao)]
    pagina_id = ag.get("pagina_id")
    execucao = await _execucao_pagina(conn, pagina_id) if pagina_id else None
    opcfg = next(
        (
            o
            for o in ((execucao or {}).get("operador") or [])
            if isinstance(o, dict) and o.get("tipo") == "operacao"
        ),
        None,
    )
    if not opcfg or not opcfg.get("param"):
        return [base]
    op_param = opcfg["param"]
    if isinstance(selecao, list) and selecao:
        ids = [str(x) for x in selecao]
    else:
        ids = await _todas_operacoes(conn, opcfg)
    return [{**base, op_param: i} for i in ids]


# Postponement: the firing found the previous execution (same parameters) STILL in flight. Instead
# of skipping the occurrence (a plain CronJob `Forbid`), the schedule comes back in
# `ADIAMENTO_MINUTOS`, at most `TETO_ADIAMENTOS` times; once exhausted it is `erro` and the schedule
# follows the cron. Example: a daily 03:00 refresh that was also fired by hand at 02:54; skipping
# would leave the day without a refresh if the manual run failed.
ADIAMENTO_MINUTOS = 5
TETO_ADIAMENTOS = 6
_RE_ADIADO = re.compile(r"^adiado \((\d+)/\d+\)$")


def _proximo_adiamento(ultimo_status: str | None) -> int:
    """How many CONSECUTIVE postponements have happened (read from the previous `ultimo_status`) + 1."""
    m = _RE_ADIADO.match(ultimo_status or "")
    return (int(m.group(1)) if m else 0) + 1


async def _registrar_falha_de_disparo(ag: dict, falhas: list[Exception], *, pedidas: int) -> None:
    """Leaves a TRACE of a scheduled firing that failed before any execution existed.

    Without this, the run would fail at 01:00 and the UI would show `erro` with NOTHING to click:
    a chain that does not resolve (upstream without a published version, cycle), an invalid
    parameter and a refused credential all die BEFORE any `criar_execucao`, so there is no
    execution to carry the reason and `ultimo_status` only holds one word. The record lives in the
    audit trail because it is the only append-only place queryable PER RESOURCE that the platform
    has. Only the FAILURE is recorded here: a good firing already leaves the execution and its
    audit row, which is where the operator clicks.

    Actor `sistema`: at 01:00 nobody clicked. Parameters NEVER go into the diff: they may carry a
    credential (one of the reasons for failure) and the trail is permanent."""
    codigos = [getattr(e, "codigo", None) or type(e).__name__ for e in falhas]
    mensagens = [getattr(e, "mensagem", None) or str(e) for e in falhas]
    depois = {
        "status": "erro",
        "pipeline_id": str(ag["pipeline_id"]),
        "execucoes_pedidas": pedidas,
        "falhas": len(falhas),
        "erro_codigo": codigos[0],
        "erro": mensagens[0],
    }
    # In the fan-out each operation fails on its own: keeping only the first reason would hide that
    # many failed for one cause and one for another, and the rare one is what gets investigated.
    distintos = sorted(set(codigos))
    if len(distintos) > 1:
        depois["erro_codigos"] = distintos
    _log.error(
        "scheduled firing failed: %s",
        mensagens[0],
        extra={"extra_campos": {**depois, "agendamento_id": str(ag["id"])}},
    )
    async with transacao() as conn:
        await registrar(
            conn,
            acao=Acao.EXECUTAR,
            recurso_tipo="agendamento",
            recurso_id=ag["id"],
            ator_id=None,
            ator_tipo=AtorTipo.SISTEMA,
            antes=None,
            depois=depois,
        )


async def _disparar(ag: dict, dispatch: Dispatch) -> tuple[str, str | None, int, bool]:
    """Expands the operations and fires ONE execution per operation (fan-out). Gateway throttling
    lives in the api_query connector (concurrency semaphore + retry/backoff), so this only enqueues
    the firings without blocking the scheduler loop.

    Returns (status, last execucao_id, number fired, postpone). `postpone` is True when NOTHING was
    fired because the previous execution (same parameters) is still in flight and the postponement
    cap was not exhausted; the caller reschedules `proxima_em` for `ADIAMENTO_MINUTOS` from now.
    On a partial fan-out (some operations in flight, others fired) there is no postponement: the
    postponed ones return on the next occurrence and the status reads `disparado (n de m)`.

    OPEN COST: since firing is CHAINED, each fan-out operation runs the WHOLE chain, not just the
    root. With many operations and a heavy upstream, that means as many full runs of the heaviest
    pipeline where there used to be none, and via Temporal they are all fired without waiting for
    any to finish, hence concurrent. There is no per-chain upstream deduplication, and two
    schedules sharing an upstream also run it twice.
    """
    async with transacao() as conn:
        conjuntos = await _expandir(conn, ag)
    if not conjuntos:
        return "vazio", None, 0, False
    ultimo: str | None = None
    em_voo: str | None = None
    adiados = 0
    falhas: list[Exception] = []
    for params in conjuntos:
        try:
            res = await dispatch(
                ag["pipeline_id"], ag["criado_por"], params, ag.get("pagina_id"), ag["id"]
            )
            ultimo = res or ultimo
        except ErroAplicacao as e:
            if e.codigo != "execucao_em_andamento":
                falhas.append(e)
                continue
            adiados += 1
            # The guard points at the in-flight execution in `detalhes[0]`; that is what the UI links to.
            em_voo = next((d.mensagem for d in e.detalhes or []), em_voo)
        except Exception as exc:  # noqa: BLE001 — one operation's failure does not block the others
            falhas.append(exc)
    # Only a FAILURE leaves a trace. Postponement is the expected outcome of the duplicate-execution
    # guard: one audit row per postponed occurrence would flood the permanent record with noise.
    if falhas:
        await _registrar_falha_de_disparo(ag, falhas, pedidas=len(conjuntos))
    m = len(conjuntos)
    n = m - len(falhas) - adiados
    if n == 0 and adiados and not falhas:
        k = _proximo_adiamento(ag.get("ultimo_status"))
        if k > TETO_ADIAMENTOS:
            return "erro", em_voo, 0, False
        return f"adiado ({k}/{TETO_ADIAMENTOS})", em_voo, 0, True
    if n == 0:
        return "erro", ultimo or em_voo, 0, False
    if m == 1:
        return "disparado", ultimo, n, False
    status = f"disparado ({n})" if n == m else f"disparado ({n} de {m})"
    return status, ultimo, n, False


async def processar_devidos(dispatch: Dispatch) -> int:
    """One pass: RESERVES the due schedules, fires them, reschedules `proxima_em` and expires those
    outside their validity window. Returns how many were reserved (= fired in this pass).

    Reservation and rescheduling happen in the SAME transaction as the SELECT, with
    `FOR UPDATE SKIP LOCKED`, the same discipline as `ingestao.service.fontes_devidas`. Two worker
    replicas run the same tick at the same time: with the `SELECT` in one transaction and the
    `UPDATE proxima_em` in ANOTHER (after firing), both would read `proxima_em <= now()` and fire
    the same schedule twice: two reports, two writes from side-effect nodes, and nothing in the
    trail explaining why. `SKIP LOCKED` makes the second replica simply not see the row the first
    already reserved.

    Firing stays OUTSIDE the transaction on purpose: `_disparar` opens its own transaction and may
    take minutes in the fan-out; holding the lock until then would queue the neighbouring replica
    instead of releasing it. The outcome (`ultimo_status`/`ultima_execucao_id`/`ultimo_disparo_em`)
    is written afterwards, in one transaction per schedule, without touching `proxima_em`, which is
    already reserved.

    The price of early reservation is explicit: a firing that fails is NOT retried on the next tick
    (the schedule was already moved forward) but on the next occurrence, with `ultimo_status='erro'`
    shown in the UI. It is the same deal as `fontes_devidas`, and the right side of it: repeating a
    scheduled report also means repeating the graph's side-effect nodes (database writes, HTTP
    calls, e-mail).
    """
    async with transacao() as conn:
        devidos = [
            dict(r)
            for r in (
                (
                    await conn.execute(
                        text(
                            "SELECT id, pipeline_id, criado_por, pagina_id, parametros, agenda, vigencia_fim, ultimo_status "
                            "FROM agendamentos WHERE ativo AND proxima_em <= now() "
                            "ORDER BY proxima_em LIMIT 50 FOR UPDATE SKIP LOCKED"
                        )
                    )
                )
                .mappings()
                .all()
            )
        ]
        for ag in devidos:
            prox = proxima_em(ag["agenda"] or {})
            ativo = not (
                ag["vigencia_fim"] is not None
                and para_exibicao(prox, FUSO_SAO_PAULO).date() > ag["vigencia_fim"]
            )
            await conn.execute(
                text("UPDATE agendamentos SET proxima_em=:px, ativo=:at WHERE id = :id"),
                {"px": prox, "at": ativo, "id": ag["id"]},
            )

    for ag in devidos:
        # try PER SCHEDULE, for the same reason as the ingestion loop in the worker: the reservation
        # above already advanced `proxima_em` for ALL of them, so an exception escaping here would
        # take the following ones with it, and they would only return on the next occurrence.
        # `_disparar` already isolates failure PER OPERATION; what reaches here is a failure before
        # the fan-out (e.g. database down while expanding the period).
        try:
            status, exec_id, _n, adiar = await _disparar(ag, dispatch)
        except Exception:  # noqa: BLE001 — one schedule does not block the others
            status, exec_id, adiar = "erro", None, False
        async with transacao() as conn:
            await conn.execute(
                text(
                    "UPDATE agendamentos SET ultimo_status=:st, ultima_execucao_id=:ex, "
                    "ultimo_disparo_em=now() WHERE id = :id"
                ),
                {"st": status, "ex": exec_id, "id": ag["id"]},
            )
            if adiar:
                # Overrides the reservation made above (next cron occurrence) with "soon": the
                # previous one is still running, and the occurrence is neither lost nor piled up.
                await conn.execute(
                    text(
                        "UPDATE agendamentos SET proxima_em = now() + make_interval(mins => :m) "
                        "WHERE id = :id AND ativo"
                    ),
                    {"m": ADIAMENTO_MINUTOS, "id": ag["id"]},
                )
    return len(devidos)


async def executar_agora(
    agendamento_id: uuid.UUID, dispatch: Dispatch, *, ator_id: uuid.UUID, ip=None, request_id=None
) -> dict:
    """Fires the schedule NOW without touching `proxima_em` (test/force). Audits the firing."""
    async with transacao() as conn:
        ag = (
            (
                await conn.execute(
                    text(
                        "SELECT id, pipeline_id, criado_por, pagina_id, parametros FROM agendamentos WHERE id = :id"
                    ),
                    {"id": agendamento_id},
                )
            )
            .mappings()
            .first()
        )
        if ag is None:
            raise ErroAplicacao(codigo="nao_encontrado", mensagem="Schedule not found.", status=404)
    status, exec_id, n, _adiar = await _disparar(dict(ag), dispatch)
    async with transacao() as conn:
        await conn.execute(
            text(
                "UPDATE agendamentos SET ultimo_status=:st, ultima_execucao_id=:ex, "
                "ultimo_disparo_em=now() WHERE id=:id"
            ),
            {"st": status, "ex": exec_id, "id": agendamento_id},
        )
        await registrar(
            conn,
            acao=Acao.EXECUTAR,
            recurso_tipo="agendamento",
            recurso_id=agendamento_id,
            ator_id=ator_id,
            antes=None,
            depois={"executar_agora": True, "execucao_id": exec_id, "execucoes": n},
            request_id=request_id,
            ip=ip,
        )
    return {"status": status, "execucao_id": exec_id, "execucoes": n}


def _json(v) -> str:
    return json.dumps(v, default=str)
