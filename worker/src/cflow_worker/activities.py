"""Activities do Temporal: cada nó do grafo = uma activity. Corpo reusa o engine de execução
(snapshot por nó). O Temporal é infraestrutura invisível — este vocabulário não vaza na API."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import timedelta

from temporalio import activity

from cflow_core.execucao import engine

# Activity sem heartbeat só é dada como morta no `start_to_close` (30 min): um pod que cai no meio
# de um nó prende a execução por meia hora. Com heartbeat a cada 20 s e timeout de 2 min, o
# Temporal libera e (nó comum) retenta noutra réplica — que lê o predecessor do volume compartilhado.
# Moram aqui (e não em `workflow.py`) porque o workflow importa as activities, não o contrário.
INTERVALO_HEARTBEAT_S = 20
HEARTBEAT_TIMEOUT = timedelta(minutes=2)


async def _bater(no_id: str) -> None:
    """Heartbeat periódico enquanto o nó roda. Só funciona porque o trabalho pesado sai do loop
    (`bloqueante.em_thread`) — com o loop congelado por um DuckDB síncrono, esta task nunca
    rodaria e o timeout de heartbeat mataria justamente o nó mais pesado."""
    while True:
        activity.heartbeat(no_id)
        await asyncio.sleep(INTERVALO_HEARTBEAT_S)


@activity.defn
async def executar_no_act(
    execucao_id: str,
    definicao: dict,
    no_id: str,
    parametros: dict | None = None,
    fontes_execucao: dict | None = None,
    forcar: bool = False,
    sem_publicar: bool = False,
) -> dict:
    # `sem_publicar` por último e com default: activity agendada antes deste campo chega com seis
    # argumentos e continua rodando igual.
    batedor = asyncio.create_task(_bater(no_id))
    try:
        return await engine.executar_no(
            execucao_id=uuid.UUID(execucao_id),
            definicao=definicao,
            no_id=no_id,
            parametros=parametros,
            fontes_execucao=fontes_execucao,
            forcar=forcar,
            sem_publicar=sem_publicar,
        )
    finally:
        batedor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await batedor


@activity.defn
async def finalizar_act(
    execucao_id: str,
    erro: str | None,
    somente_se_executando: bool = False,
    sem_publicar: bool = False,
) -> None:
    """`somente_se_executando` é usado pela limpeza da cadeia (`ExecutarDAG`), que varre execuções
    pré-criadas: sem ele, o UPDATE cego reescreveria como falha um irmão que já concluiu. O default
    `False` preserva o contrato de quem já chama com dois argumentos. `sem_publicar` (montante de
    raiz com `montante_publica: false`) desliga o relatório padrão, que também é objeto no lake."""
    await engine.finalizar(
        execucao_id=uuid.UUID(execucao_id),
        erro=erro,
        somente_se_executando=somente_se_executando,
        sem_publicar=sem_publicar,
    )
