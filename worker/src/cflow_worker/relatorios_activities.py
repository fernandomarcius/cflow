"""Activity da geração de relatório : UM arquivo = UMA activity. Corpo reusa
`cflow_core.relatorios.service.gerar`, que fecha o registro como `pronto` ou `falha` sozinho — a
activity nunca levanta por causa do dado, para o Temporal não insistir num arquivo que vai falhar
igual. O vocabulário do Temporal continua invisível."""

from __future__ import annotations

from temporalio import activity

from cflow_core.relatorios import service as relatorios


@activity.defn
async def gerar_relatorio_act(exportacao_id: str) -> dict:
    return await relatorios.gerar(exportacao_id)
