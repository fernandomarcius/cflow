"""Fila própria dos relatórios: um arquivo = uma activity, sem retentativa por fora (quem decide o
desfecho é `service.gerar`, que grava `pronto`/`falha`). Sem Temporal nem banco aqui — o que se
confere é a fiação que o worker de produção e o de teste compartilham."""

from __future__ import annotations

from cflow_core.config import Settings
from cflow_worker import relatorios_activities, relatorios_workflow


def _default(campo: str) -> str:
    # O DEFAULT do campo, não uma instância: na mesma sessão de pytest o conftest da API já pôs
    # `cflow-relatorios-test` no ambiente, e é esse isolamento que se quer preservar — não testar.
    return Settings.model_fields[campo].default


def test_fila_propria_e_a_mesma_do_config():
    assert relatorios_workflow.TASK_QUEUE == "cflow-relatorios"
    assert _default("relatorios_task_queue") == relatorios_workflow.TASK_QUEUE
    # isolada das outras duas filas — é o motivo de existir
    filas = {
        _default("temporal_task_queue"),
        _default("ingestao_task_queue"),
        _default("relatorios_task_queue"),
    }
    assert len(filas) == 3


def test_um_arquivo_uma_activity_sem_retentativa():
    assert relatorios_workflow.POLITICA.maximum_attempts == 1
    assert hasattr(relatorios_activities.gerar_relatorio_act, "__temporal_activity_definition")
    assert hasattr(relatorios_workflow.GerarRelatorio, "__temporal_workflow_definition")


def test_concorrencia_padrao_e_ao_menos_quatro():
    assert _default("relatorios_concorrencia") >= 4
