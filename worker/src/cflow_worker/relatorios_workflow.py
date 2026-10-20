"""Workflow de geração de relatório: uma exportação = um workflow = uma activity, na task queue
PRÓPRIA dos relatórios (`cflow-relatorios`). Determinístico — só chama a activity.

Why a dedicated queue: a large export (around a million rows) takes minutes to become an xlsx
file, and publishing a batch produces dozens of artifacts. On the pipeline queue they would push
the next scheduled execution back; on the ingestion queue, the periodic reload.

Sem retentativa (`maximum_attempts=1`): quem decide o desfecho é `service.gerar`, que grava
`pronto`/`falha` no registro. Retentar por fora produziria uma segunda tentativa invisível — e a
falha por dado (coluna que sumiu, valor inválido) não melhora repetindo.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .relatorios_activities import gerar_relatorio_act

TASK_QUEUE = "cflow-relatorios"
POLITICA = RetryPolicy(maximum_attempts=1)
# Worst case measured is a couple of minutes on a laptop; leave generous headroom for slower hosts.
TIMEOUT = timedelta(minutes=60)


@workflow.defn
class GerarRelatorio:
    @workflow.run
    async def run(self, exportacao_id: str) -> str:
        resultado = await workflow.execute_activity(
            gerar_relatorio_act,
            args=[exportacao_id],
            start_to_close_timeout=TIMEOUT,
            retry_policy=POLITICA,
        )
        return str((resultado or {}).get("estado", "falha"))
