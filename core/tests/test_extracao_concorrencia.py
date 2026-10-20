"""Cap on simultaneous full SQL Server extractions per process (`extracao_concorrencia_max`).
Several large extractions at once over a VPN can stall the tunnel. Without the setting,
`worker_max_atividades` applies."""

from __future__ import annotations

import asyncio

from cflow_core.conexoes.colunar import Extracao
from cflow_core.conexoes.conectores import sqlserver
from cflow_core.config import get_settings


async def _pico(monkeypatch, teto: str | None, n: int = 6) -> int:
    estado = {"em_voo": 0, "pico": 0}

    async def _extrair(self, *_a, **_k):
        estado["em_voo"] += 1
        estado["pico"] = max(estado["pico"], estado["em_voo"])
        await asyncio.sleep(0.02)
        estado["em_voo"] -= 1
        return Extracao(["x"], linhas=[])

    monkeypatch.setattr(sqlserver.ConectorSqlServer, "_extrair_tabela", _extrair)
    if teto is None:
        monkeypatch.delenv("CFLOW_EXTRACAO_CONCORRENCIA_MAX", raising=False)
    else:
        monkeypatch.setenv("CFLOW_EXTRACAO_CONCORRENCIA_MAX", teto)
    get_settings.cache_clear()
    try:
        c = sqlserver.ConectorSqlServer()
        await asyncio.gather(*(c.extrair_tabela({}, None, "SELECT 1") for _ in range(n)))
    finally:
        get_settings.cache_clear()
    return estado["pico"]


async def test_teto_limita_extracoes_simultaneas(monkeypatch):
    assert await _pico(monkeypatch, "2") == 2


async def test_sem_setting_o_teto_e_o_numero_de_activities(monkeypatch):
    """Without the setting the previous limit applies: extractions in flight were bounded by the
    DuckDB slots (`worker_max_atividades`), which the source held during extraction."""
    monkeypatch.setenv("CFLOW_WORKER_MAX_ATIVIDADES", "3")
    assert await _pico(monkeypatch, None, n=8) == 3
