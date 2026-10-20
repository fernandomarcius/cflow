"""Activities da ingestão: uma fatia = uma activity. Corpo reusa `cflow_core.ingestao`.

O Temporal continua invisível — este vocabulário não vaza para a API nem para a UI.
"""

from __future__ import annotations

from temporalio import activity

from cflow_core.ingestao import service as ingestao


@activity.defn
async def ingerir_fatia_act(
    fonte_id: str, execucao_id: str, fatia: dict, replay: bool = False
) -> dict:
    return await ingestao.processar_uma_fatia(
        fonte_id=fonte_id, execucao_id=execucao_id, fatia_ctx=fatia, replay=replay
    )


@activity.defn
async def concluir_ingestao_act(
    fonte_id: str, execucao_id: str, contagens: dict, fatias: list[dict]
) -> None:
    await ingestao.concluir(
        fonte_id=fonte_id, execucao_id=execucao_id, contagens=contagens, fatias_ctx=fatias
    )


@activity.defn
async def falhar_ingestao_act(execucao_id: str, erro: str, contagens: dict) -> None:
    # `ingestao.falhar`, não `repo.finalizar_execucao` direto: o segundo grava a falha no banco e
    # NÃO emite `ingestao_execucao_concluida`. Como `CFLOW_USAR_TEMPORAL=true` é o padrão, esta é a
    # activity que fecha toda execução falha em produção — e o log ficava mudo exatamente no
    # evento que alguém vem investigar. O caminho inline já logava; este não.
    #
    # As contagens do que JÁ pousou continuam gravadas: uma execução que trouxe 8 de 10 fatias e
    # caiu na nona não é "zero" — e o watermark (que só `concluir` avança) fica onde está.
    await ingestao.falhar(execucao_id=execucao_id, erro=erro, contagens=contagens)
