"""Time zone helpers. Database in UTC (timestamptz); display in America/Sao_Paulo at the edge."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

FUSO_SAO_PAULO = ZoneInfo("America/Sao_Paulo")


def agora_utc() -> datetime:
    """Current moment in UTC, time zone aware."""
    return datetime.now(tz=UTC)


def para_exibicao(momento: datetime, fuso: ZoneInfo = FUSO_SAO_PAULO) -> datetime:
    """Converts an instant to the display time zone, always with an explicit offset.

    Naive datetimes (no tzinfo) are assumed to be UTC, the database convention.
    """
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=UTC)
    return momento.astimezone(fuso)
