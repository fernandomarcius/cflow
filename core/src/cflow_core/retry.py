"""HTTP retry: what is worth repeating and how long to wait first.

It exists because the SAME backoff formula was copied in several places (ingestion connectors,
the `api_query` connector, the lake backend retry). Several copies of one decision are several
places for it to diverge, and the one that diverges silently is the one that makes a backfill
hammer the origin.

What this module does NOT decide: how many attempts (up to the caller, because the cost of one
more attempt varies: on object storage it costs a request, on a public origin it costs IP
reputation) nor what to do with the response. It only holds "is this status worth another
attempt?" and "wait this long".
"""

from __future__ import annotations

import random

# Statuses worth another attempt. Everything else is the server's answer about the REQUEST, not
# about the moment: 400/401/403/404/409 do not improve by insisting, and 412 (`ifGenerationMatch=0`
# refusing because the object already exists) is by definition the right answer; repeating it
# would be asking immutability to stop holding.
RETENTAVEIS = frozenset({408, 429, 500, 502, 503, 504})

# Ceiling of one wait. Without a ceiling, the 6th attempt of an exponential backoff sleeps 64 s
# inside an activity, and its timeout is spent waiting instead of working.
TETO_PADRAO_SEGUNDOS = 20.0

# Jitter: two slices that failed on the same 503 would retry in the same millisecond and
# reproduce the burst that caused the 503.
_JITTER_MAX = 0.5


def retry_after_segundos(valor: str | None, *, agora: float | None = None) -> float | None:
    """Converts the `Retry-After` header into seconds. Accepts BOTH RFC 9110 forms: a number of
    seconds (`120`) and an HTTP date (`Wed, 21 Oct 2026 07:28:00 GMT`).

    Returns `None` when the header is absent or unreadable, in which case the caller uses the
    backoff. A date in the past becomes `0.0` (the server is saying "go ahead"), never a negative
    number, which would become an invalid `sleep`.
    """
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto:
        return None
    try:
        return max(0.0, float(int(texto)))
    except ValueError:
        pass
    try:
        import time
        from email.utils import parsedate_to_datetime

        quando = parsedate_to_datetime(texto)
    except (TypeError, ValueError):
        return None
    if quando is None:
        return None
    if quando.tzinfo is None:
        # An HTTP date without a zone is GMT by definition (RFC 9110); assuming the local zone
        # here would give a wait of hours (or negative) depending on the worker's server.
        from datetime import UTC

        quando = quando.replace(tzinfo=UTC)
    referencia = agora if agora is not None else time.time()
    return max(0.0, quando.timestamp() - referencia)


def espera(
    tentativa: int,
    *,
    teto: float = TETO_PADRAO_SEGUNDOS,
    retry_after: str | None = None,
) -> float:
    """How many seconds to sleep before the next attempt (`tentativa` is 0-based).

    Exponential backoff with ceiling and jitter. When the server SENDS a `Retry-After`, it wins:
    it knows when the quota reopens and our exponential is a guess. But it is still subject to
    the ceiling: a `Retry-After: 3600` from a misconfigured proxy would hold the activity for
    an hour.
    """
    limite = max(0.0, float(teto))
    pedido = retry_after_segundos(retry_after)
    base = min(pedido, limite) if pedido is not None else min(2.0**tentativa, limite)
    return base + random.uniform(0, _JITTER_MAX)
