"""Unit: time zone conversion."""

from __future__ import annotations

from datetime import UTC, datetime

from cflow_core.timezone import FUSO_SAO_PAULO, agora_utc, para_exibicao


def test_agora_utc_tem_tzinfo_utc():
    a = agora_utc()
    assert a.tzinfo is UTC


def test_para_exibicao_converte_para_sao_paulo():
    # 2026-06-17 12:00 UTC -> 09:00 in Sao Paulo (UTC-3, no daylight saving)
    utc = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)
    sp = para_exibicao(utc)
    assert sp.tzinfo == FUSO_SAO_PAULO
    assert sp.hour == 9
    assert sp.utcoffset().total_seconds() == -3 * 3600


def test_para_exibicao_assume_utc_para_naive():
    naive = datetime(2026, 6, 17, 12, 0)
    sp = para_exibicao(naive)
    assert sp.hour == 9
