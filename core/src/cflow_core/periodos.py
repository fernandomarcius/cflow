"""Resolution of date periods (relative and fixed) -> half-open window [start, end) in ISO
YYYY-MM-DD, always relative to "today" in America/Sao_Paulo (explicit time zone).

SINGLE source of truth for date resolution; the web portal mirrors it in
`web/src/cflow/periodos.ts` (keep both in sync; parity tests exist). Used at execution time (the
`competencia` parameter type delegates here) and at schedule firing (the relative token stored in
the definition is resolved into concrete dates when it fires).

Conventions:
- Half-open window [start, end): `end` is the 1st day OUTSIDE the period (SQL filters `date < :end`).
- Week = Sunday->Saturday: the week containing `today` starts on Sunday (on `today` itself if it is
  Sunday) and ends on Saturday (end = the following Sunday, exclusive).
- A custom range "A..B" has BOTH ends inclusive -> resolves to [A, B+1 day).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .timezone import FUSO_SAO_PAULO, agora_utc, para_exibicao

# "YYYY-MM", matched WHOLE and with ASCII digits only. `\d` would match wide digits
# ("２０２６-08", which Python's `int()` still reads as 2026) and `$` accepts a trailing "\n": both
# passed as a valid period, and any adapter that must agree with this rule case by case would
# refuse them.
_YM = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
_INTERVALO = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")  # "YYYY-MM-DD..YYYY-MM-DD"

# Recognized relative tokens (besides "YYYY-MM" and "YYYY-MM-DD..YYYY-MM-DD").
TOKENS_RELATIVOS = (
    "mes_atual",
    "mes_anterior",
    "semana_atual",
    "semana_anterior",
    "hoje",
    "ontem",
    "ultimos_7_dias",
    "ultimos_30_dias",
)


def hoje_sao_paulo() -> date:
    """Current day in America/Sao_Paulo (explicit time zone)."""
    return para_exibicao(agora_utc(), FUSO_SAO_PAULO).date()


def _proximo_mes(primeiro: date) -> date:
    return (
        date(primeiro.year + 1, 1, 1)
        if primeiro.month == 12
        else date(primeiro.year, primeiro.month + 1, 1)
    )


def _domingo_da_semana(d: date) -> date:
    """Sunday that starts the week of `d` (Sunday->Saturday). weekday(): Mon=0..Sun=6."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def resolver_janela(token: str, hoje: date | None = None) -> tuple[str, str]:
    """Resolves a period token into the half-open window [start, end) -> (ISO, ISO).

    Accepts the relative tokens (TOKENS_RELATIVOS, plus the month aliases 'atual'/'anterior'),
    "YYYY-MM" (a specific month) and "YYYY-MM-DD..YYYY-MM-DD" (custom range, inclusive ends).
    Raises ValueError if the token is not recognized (the caller converts it into an API error)."""
    t = str(token).strip()
    h = hoje or hoje_sao_paulo()

    if t in ("mes_atual", "atual"):
        ini = h.replace(day=1)
        return ini.isoformat(), _proximo_mes(ini).isoformat()
    if t in ("mes_anterior", "anterior"):
        prim = h.replace(day=1)
        ant = date(prim.year - 1, 12, 1) if prim.month == 1 else date(prim.year, prim.month - 1, 1)
        return ant.isoformat(), prim.isoformat()
    if t == "semana_atual":
        ini = _domingo_da_semana(h)
        return ini.isoformat(), (ini + timedelta(days=7)).isoformat()
    if t == "semana_anterior":
        fim = _domingo_da_semana(h)
        return (fim - timedelta(days=7)).isoformat(), fim.isoformat()
    if t == "hoje":
        return h.isoformat(), (h + timedelta(days=1)).isoformat()
    if t == "ontem":
        return (h - timedelta(days=1)).isoformat(), h.isoformat()
    if t == "ultimos_7_dias":
        return (h - timedelta(days=6)).isoformat(), (h + timedelta(days=1)).isoformat()
    if t == "ultimos_30_dias":
        return (h - timedelta(days=29)).isoformat(), (h + timedelta(days=1)).isoformat()
    if _YM.fullmatch(t):
        ano, mes = (int(p) for p in t.split("-"))
        ini = date(ano, mes, 1)
        return ini.isoformat(), _proximo_mes(ini).isoformat()
    m = _INTERVALO.match(t)
    if m:
        ini = date.fromisoformat(m.group(1))
        fim_incl = date.fromisoformat(m.group(2))
        if fim_incl < ini:
            raise ValueError(token)
        return ini.isoformat(), (fim_incl + timedelta(days=1)).isoformat()
    raise ValueError(token)


def eh_competencia(valor: object) -> bool:
    """The value has the SHAPE of a period: literal "YYYY-MM" or the tokens "atual"/"anterior".

    It is the predicate of the `competencia` parameter and of the calendar-derived one, and both
    must be the SAME: if the period passed one check and the derived computation refused the same
    text in the other, the derived value would come out empty with the period accepted, and the
    SQL would bind NULL, which means "bring everything". No `strip`: surrounding whitespace is
    not a period."""
    return isinstance(valor, str) and (valor in ("atual", "anterior") or bool(_YM.fullmatch(valor)))


def eh_relativo(valor: object) -> bool:
    """Token whose meaning depends on TODAY (`atual`, `anterior`, `hoje`, `mes_atual`...): resolved
    tomorrow, it gives another period. Literal months, dates and ranges do not. It tells whether
    an input can be used to recognize a run later: the same input normalizes differently."""
    return isinstance(valor, str) and valor.strip() in (*TOKENS_RELATIVOS, "atual", "anterior")


def deslocar_competencia(valor: str, meses: int, hoje: date | None = None) -> str:
    """The period `valor` shifted by `meses` months, on the 1st day of the month: "YYYY-MM-01".

    It is the computation of the calendar-derived parameter: `2026-08` with `-1` gives
    `2026-07-01`, the same shape as a `competencia` partition. Tokens ("atual"/"anterior") are
    relative to `hoje` in America/Sao_Paulo, as in the `competencia` parameter.

    Raises ValueError if the value does not have the period shape or if the result would fall
    outside years 1..9999; `date` cannot represent year 0."""
    v = str(valor)
    if not eh_competencia(v):
        raise ValueError(valor)
    # A literal "YYYY-MM" is read directly, without going through the WINDOW:
    # `resolver_janela("9999-12")` overflows at the period end (year 10000), and the computation
    # does not need it. A token resolves from today's date.
    if v in ("atual", "anterior"):
        inicio, _fim = resolver_janela(v, hoje)
        ano_ini, mes_ini = int(inicio[:4]), int(inicio[5:7])
    else:
        ano_ini, mes_ini = int(v[:4]), int(v[5:7])
    if ano_ini < 1:
        raise ValueError(valor)  # year 0000 does not exist in `date`
    total = ano_ini * 12 + mes_ini - 1 + int(meses)
    ano, mes0 = divmod(total, 12)
    if not 1 <= ano <= 9999:
        raise ValueError(f"{valor} {int(meses):+d} meses")
    return f"{ano:04d}-{mes0 + 1:02d}-01"


_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?")
_DATA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def resolver_corte(valor: str, hoje: date | None = None) -> str:
    """Resolves an **as-of** cutoff value (system/transaction time) into an INCLUSIVE ISO
    TIMESTAMP 'YYYY-MM-DD HH:MM:SS[.ffffff]'. Used by the `as_of` parameter: the source node
    filters `record_date <= :cutoff` to see the data "as it was" up to that instant.

    Accepts:
    - **literal timestamp** ('YYYY-MM-DD HH:MM:SS', with 'T'/fractions) -> exact point in time;
    - **literal date** ('YYYY-MM-DD') -> last instant of that day;
    - **period token** / 'YYYY-MM' (delegates to `resolver_janela`) -> INCLUSIVE last instant of
      the period, SECOND precision (window end is exclusive -> end - 1s). E.g. '2026-05' ->
      '2026-05-31 23:59:59'; 'hoje' -> today 23:59:59 (live).

    Second precision (no fractions): compatible with SQL Server `datetime`. Intraday granularity
    comes from the literal timestamp.
    Raises ValueError if not recognized (the caller converts it into an API error).
    """
    v = str(valor).strip()
    if _TIMESTAMP.match(v):
        return v.replace("T", " ")
    if _DATA.match(v):
        return f"{v} 23:59:59"
    _, fim = resolver_janela(v, hoje)  # EXCLUSIVE ISO date (1st day outside the period)
    ultimo = datetime.fromisoformat(fim) - timedelta(seconds=1)
    return ultimo.isoformat(sep=" ")
