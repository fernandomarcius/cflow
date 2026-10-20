"""Workflow de ingestão: uma execução = N fatias, cada fatia = uma activity.

Determinístico — só decide ordem/concorrência e chama activities; todo IO (rede, DuckDB, lake,
Postgres) mora nas activities. Roda na task queue **própria da ingestão** (`cflow-ingestao`), longe
da fila dos pipelines: um backfill de dez anos são centenas de activities, e na fila compartilhada
elas empurrariam a execução do relatório de amanhã para trás.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .ingestao_activities import concluir_ingestao_act, falhar_ingestao_act, ingerir_fatia_act

TASK_QUEUE = "cflow-ingestao"

# A fatia É idempotente por construção (hash do payload contra o índice único): retentar é seguro,
# e é o que resolve o caso comum — a origem pública que devolve 502 no meio da tarde.
POLITICA = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5))


@workflow.defn
class IngerirFonte:
    @workflow.run
    async def run(
        self,
        fonte_id: str,
        execucao_id: str,
        fatias: list[dict],
        replay: bool = False,
        concorrencia: int = 4,
    ) -> str:
        contagens: dict = {}
        janela = max(1, min(int(concorrencia or 1), 8))
        try:
            # Lotes de `concorrencia` fatias: as fontes públicas pedem teto (some APIs accept at most 5
            # simultâneas) e um fan-out irrestrito de um backfill vira bloqueio de IP.
            for i in range(0, len(fatias), janela):
                bloco = fatias[i : i + janela]
                resultados = await asyncio.gather(
                    *(
                        workflow.execute_activity(
                            ingerir_fatia_act,
                            args=[fonte_id, execucao_id, fatia, replay],
                            start_to_close_timeout=timedelta(minutes=30),
                            retry_policy=POLITICA,
                        )
                        for fatia in bloco
                    )
                )
                for parcial in resultados:
                    for k, v in (parcial or {}).items():
                        contagens[k] = contagens.get(k, 0) + v

            await workflow.execute_activity(
                concluir_ingestao_act,
                args=[fonte_id, execucao_id, contagens, fatias],
                start_to_close_timeout=timedelta(minutes=2),
            )
            return "concluida"
        except Exception:
            await workflow.execute_activity(
                falhar_ingestao_act,
                args=[execucao_id, "Falha na ingestão de uma fatia.", contagens],
                start_to_close_timeout=timedelta(minutes=2),
            )
            raise
