"""Worker Temporal: registra o workflow de execução do grafo e as activities (nós).

O Temporal é infraestrutura invisível — Studio/Portal nunca falam com ele; só a API,
como cliente, dispara o workflow. Este processo roda em contêiner.

Os dois laços cadenciados (agendador de pipelines e cadência das fontes externas) vivem em
funções de MÓDULO, não em closures dentro de `main()`: como closure, um `for` sem `try` por item
não tinha como ser testado sem subir Temporal e Postgres — e foi exatamente ali que a fonte de
ingestão perdia o dia (ver `tick_ingestao`). `main()` é só fiação.

Ciclo de vida (compatível com orquestradores de contêiner como Kubernetes ou Docker Compose):

- `GET /healthz` em `CFLOW_HEALTHZ_PORTA` (8080; `0` desliga) — ver `saude.py`;
- a conexão com o Temporal é retentada com recuo até conectar (a sonda fica 503 enquanto isso),
  em vez de o processo morrer na subida porque o servidor ainda não está de pé;
- SIGTERM/SIGINT DRENAM: a sonda vira 503, os laços cadenciados param depois da passada corrente,
  os workers deixam de pegar tarefa e as activities em voo têm `CFLOW_WORKER_DRENAGEM_SEGUNDOS`
  para terminar antes de serem canceladas (o Temporal as retenta noutra réplica).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

from cflow_core.config import get_settings
from cflow_core.logging import configurar_logging

from . import saude

logger = logging.getLogger("cflow_worker")

# Intervalo entre passadas de cada laço, em segundos.
INTERVALO_AGENDADOR = 30
INTERVALO_INGESTAO = 60
INTERVALO_RECONCILIACAO = 300
# Idade mínima de uma execução `executando` para ser candidata a órfã (ver `reconciliacao`).
IDADE_ORFA_MINUTOS = 30

# Configuração DO PROCESSO do worker — não é setting do `cflow-core` porque nenhum outro serviço a lê.
PORTA_SAUDE_PADRAO = 8080
# O Kubernetes espera 30 s (padrão) entre o SIGTERM e o SIGKILL; a drenagem cabe folgada nisso.
DRENAGEM_PADRAO_SEGUNDOS = 20
RECONEXAO_TETO_SEGUNDOS = 30.0


async def tick_agendador(dispatch) -> int:
    """Uma passada do agendador de pipelines. Devolve quantos foram disparados.

    NUNCA propaga: o laço do worker é um `while True` e uma exceção que escapa mata o agendador
    do processo inteiro, em silêncio — os pipelines simplesmente param de rodar e o worker segue
    respondendo saudável.
    """
    from cflow_core.execucao import agendador

    try:
        n = await agendador.processar_devidos(dispatch)
    except Exception:  # noqa: BLE001 — falha de uma passada não pode matar o laço
        logger.exception("agendador_erro")
        return 0
    if n:
        logger.info("agendador_disparou", extra={"extra_campos": {"quantos": n}})
    return n


async def tick_ingestao(
    executar: Callable[..., Awaitable], listar_devidas: Callable[[], Awaitable[list[dict]]]
) -> dict:
    """Uma passada da cadência das fontes externas. Devolve `{disparadas, erros}`.

    O `try/except` é POR FONTE, e essa é a razão de a função existir. `fontes_devidas` já avançou
    `proxima_em` de TODAS as devidas na mesma transação, ANTES de devolver a lista (é assim que
    dois ticks não disparam a mesma fonte duas vezes). Com um `try` só em volta do `for`, a
    primeira fonte que levantasse abortava o laço inteiro — e as fontes seguintes, já reservadas,
    não voltavam no tick seguinte: só na próxima cadência. Numa fonte diária isso é o dia perdido,
    sem nada na tela dizendo que houve falha em mais de uma.

    Falha de uma fonte é contabilizada e o laço SEGUE. A fonte que falhou não é retomada aqui de
    propósito: quem refaz fatia com desfecho `falha`/`quarentena` é a retentativa por fatia
    (`POST /ingestao/execucoes/{id}/retentar`), conjunto fechado e auditado.
    """
    try:
        devidas = await listar_devidas()
    except Exception:  # noqa: BLE001 — banco fora não pode matar o laço
        logger.exception("ingestao_agendador_erro")
        return {"disparadas": 0, "erros": 0}

    disparadas, erros = 0, 0
    for fonte in devidas:
        try:
            await executar(fonte_id=fonte["id"], origem="agendada", ator_id=None)
        except Exception:  # noqa: BLE001 — uma fonte não derruba as demais
            erros += 1
            logger.exception(
                "ingestao_fonte_erro", extra={"extra_campos": {"fonte": fonte.get("nome")}}
            )
            continue
        disparadas += 1
        logger.info("ingestao_disparada", extra={"extra_campos": {"fonte": fonte.get("nome")}})
    return {"disparadas": disparadas, "erros": erros}


async def tick_reconciliacao(reconciliar: Callable[..., Awaitable[dict]]) -> dict:
    """Uma passada do reaper de execução órfã. Devolve o resumo da rotina (ou `{erro}`).

    Antes disto o reaper só rodava por `scripts/reconciliar_execucoes.py`, à mão — e uma linha
    `executando` cujo workflow morreu ficava assim até alguém lembrar do script. Com o guard de
    execução duplicada (`criar_execucao`), essa linha passaria a BLOQUEAR o disparo seguinte do
    mesmo pipeline; o laço é o que garante que o bloqueio dura minutos, não dias.

    Mesma regra dos outros ticks: nunca propaga. `reconciliar` entra por injeção
    (`reconciliacao.reconciliar_com_trava`) para o tick ser testável sem banco nem Temporal.
    """
    try:
        r = await reconciliar(idade_minima_minutos=IDADE_ORFA_MINUTOS)
    except Exception:  # noqa: BLE001 — falha de uma passada não pode matar o laço
        logger.exception("reconciliacao_erro")
        return {"erro": True, "fechadas": 0, "vivas": 0}
    if r.get("fechadas"):
        logger.info(
            "reconciliacao_fechou_orfas", extra={"extra_campos": {"quantas": r["fechadas"]}}
        )
    return r


def _runtime(settings):
    """Runtime do SDK com métricas Prometheus quando `CFLOW_METRICAS_PORTA` está definida. É de
    onde saem `temporal_worker_task_slots_available` (saturação real do worker) e
    `temporal_activity_schedule_to_start_latency` (quanto a activity esperou na fila) — os dois
    sinais que dizem se falta réplica, e que o KEDA/HPA leem."""
    if not settings.metricas_porta:
        return None
    from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig

    return Runtime(
        telemetry=TelemetryConfig(
            metrics=PrometheusConfig(bind_address=f"0.0.0.0:{int(settings.metricas_porta)}")
        )
    )


class ErroConfiguracao(Exception):
    """Configuração inválida do processo: a subida para nomeando TODOS os problemas de uma vez."""


@dataclass(frozen=True)
class ConfigProcesso:
    porta_saude: int | None
    drenagem_segundos: int


def _inteiro(
    environ: Mapping[str, str], nome: str, padrao: int, minimo: int, maximo: int, erros: list[str]
) -> int:
    bruto = environ.get(nome, "").strip()
    if not bruto:
        return padrao
    try:
        valor = int(bruto)
    except ValueError:
        erros.append(f"{nome} deve ser um inteiro")
        return padrao
    if not minimo <= valor <= maximo:
        erros.append(f"{nome} deve estar entre {minimo} e {maximo}")
        return padrao
    return valor


def config_processo(
    environ: Mapping[str, str], metricas_porta: int | None = None
) -> ConfigProcesso:
    """Lê `CFLOW_HEALTHZ_PORTA` (0 desliga a sonda) e `CFLOW_WORKER_DRENAGEM_SEGUNDOS`."""
    erros: list[str] = []
    porta = _inteiro(environ, "CFLOW_HEALTHZ_PORTA", PORTA_SAUDE_PADRAO, 0, 65535, erros)
    drenagem = _inteiro(
        environ, "CFLOW_WORKER_DRENAGEM_SEGUNDOS", DRENAGEM_PADRAO_SEGUNDOS, 0, 3600, erros
    )
    if porta and metricas_porta and int(metricas_porta) == porta:
        erros.append("CFLOW_METRICAS_PORTA deve ser diferente de CFLOW_HEALTHZ_PORTA")
    if erros:
        raise ErroConfiguracao("; ".join(erros))
    return ConfigProcesso(porta_saude=porta or None, drenagem_segundos=drenagem)


async def _repetir(
    tick: Callable[[], Awaitable], intervalo: int, parar: asyncio.Event | None = None
) -> None:
    """Laço cadenciado. `tick` é quem trata o próprio erro — aqui não há `except` de propósito:
    dois lugares tratando a mesma exceção escondem qual deles engoliu o quê.

    Com `parar`, o laço termina DEPOIS da passada corrente — nunca no meio dela: cancelar um tick
    no meio podia deixar a próxima janela de uma fonte já reservada e o disparo não feito."""
    while parar is None or not parar.is_set():
        await tick()
        if parar is None:
            await asyncio.sleep(intervalo)
        else:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(parar.wait(), intervalo)


async def conectar(host: str, parar: asyncio.Event, runtime=None, conectar_fn=None):
    """Conecta ao Temporal com recuo exponencial (teto `RECONEXAO_TETO_SEGUNDOS`) até conseguir
    ou até pedirem para parar (devolve None). A sonda fica 503 enquanto isso; quem decide
    reiniciar é a plataforma, não o processo."""
    if conectar_fn is None:
        from temporalio.client import Client

        conectar_fn = Client.connect
    espera = 1.0
    while not parar.is_set():
        try:
            return await conectar_fn(host, runtime=runtime)
        except Exception as erro:  # noqa: BLE001 — o SDK levanta subclasses de RuntimeError
            logger.warning(
                "temporal_conexao_falhou",
                extra={"extra_campos": {"temporal_host": host, "erro": str(erro)}},
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(parar.wait(), espera)
        espera = min(espera * 2, RECONEXAO_TETO_SEGUNDOS)
    return None


def _instalar_sinais(parar: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sinal in (signal.SIGTERM, signal.SIGINT):

        def _pedir(sinal: int = sinal) -> None:
            if not parar.is_set():
                logger.info("worker_sinal_recebido", extra={"extra_campos": {"sinal": sinal}})
            parar.set()

        loop.add_signal_handler(sinal, _pedir)


async def rodar_ate_parar(
    workers: Sequence,
    parar: asyncio.Event,
    estado: saude.EstadoSaude,
    verificar: Callable[[], Awaitable[bool]],
    lacos: Sequence[asyncio.Task] = (),
    drenagem_segundos: float = DRENAGEM_PADRAO_SEGUNDOS,
) -> int:
    """Roda os workers até `parar` (sinal) ou até um deles morrer; depois DRENA. Devolve o código
    de saída: 0 no encerramento pedido, 1 quando um worker caiu sozinho ou falhou drenando.

    Ordem da drenagem: sonda 503 → laços cadenciados terminam a passada corrente (não disparam
    trabalho novo) → workers param de pollar e esperam as activities em voo até o
    `graceful_shutdown_timeout` → o que sobrou dos laços é cancelado."""
    estado.observar_workers(lambda: all(w.is_running for w in workers))
    monitor = asyncio.create_task(saude.monitorar(estado, verificar))
    rodando = [asyncio.create_task(w.run()) for w in workers]
    pedido = asyncio.create_task(parar.wait())
    await asyncio.wait({*rodando, pedido}, return_when=asyncio.FIRST_COMPLETED)

    estado.drenar()
    monitor.cancel()
    inesperado = not pedido.done()
    parar.set()  # os laços cadenciados param depois da passada corrente
    if inesperado:
        caidos = [
            repr(t.exception()) for t in rodando if t.done() and not t.cancelled() and t.exception()
        ]
        logger.error("worker_parou_sozinho", extra={"extra_campos": {"erros": caidos}})
    logger.info("worker_drenando", extra={"extra_campos": {"drenagem_segundos": drenagem_segundos}})

    await asyncio.gather(*(w.shutdown() for w in workers), return_exceptions=True)
    resultados = await asyncio.gather(*rodando, return_exceptions=True)
    falhas = [
        repr(r)
        for r in resultados
        if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError)
    ]
    if lacos:
        _, pendentes = await asyncio.wait(lacos, timeout=max(1.0, drenagem_segundos))
        for t in pendentes:
            t.cancel()
        await asyncio.gather(*lacos, return_exceptions=True)
    with contextlib.suppress(asyncio.CancelledError):
        await monitor
    if falhas and not inesperado:
        logger.error("worker_falhou_drenando", extra={"extra_campos": {"erros": falhas}})
    logger.info("worker_encerrado")
    return 1 if (inesperado or falhas) else 0


async def main() -> int:
    settings = get_settings()
    configurar_logging(settings.log_level)
    try:
        proc = config_processo(os.environ, settings.metricas_porta)
    except ErroConfiguracao as erro:
        logger.error("worker_configuracao_invalida", extra={"extra_campos": {"erro": str(erro)}})
        return 2

    from temporalio.worker import Worker

    from .activities import executar_no_act, finalizar_act
    from .ingestao_activities import (
        concluir_ingestao_act,
        falhar_ingestao_act,
        ingerir_fatia_act,
    )
    from .ingestao_workflow import IngerirFonte
    from .relatorios_activities import gerar_relatorio_act
    from .relatorios_workflow import GerarRelatorio
    from .workflow import ExecutarDAG, ExecutarPipeline

    parar = asyncio.Event()
    _instalar_sinais(parar)
    estado = saude.EstadoSaude()
    servidor = None
    if proc.porta_saude:
        servidor = await saude.servir(estado, "0.0.0.0", proc.porta_saude)  # noqa: S104 — sonda do pod
        logger.info("sonda_escutando", extra={"extra_campos": {"porta": proc.porta_saude}})
    try:
        task_queue = settings.temporal_task_queue
        logger.info(
            "worker_conectando", extra={"extra_campos": {"temporal_host": settings.temporal_host}}
        )
        client = await conectar(settings.temporal_host, parar, _runtime(settings))
        if client is None:
            logger.info("worker_encerrado_sem_conectar")
            return 0
        drenagem = timedelta(seconds=proc.drenagem_segundos)
        # Teto de activities simultâneas = teto de threads do executor. As activities são
        # `async def`, mas o trabalho pesado (DuckDB, sandbox) sai do loop via `bloqueante.em_thread`
        # para o executor padrão do loop — sem isto, um nó CPU-bound congelava as outras vagas e o
        # worker tinha paralelismo real ≈ 1. O throttle fino do gateway continua no conector api_query.
        max_atividades = max(1, int(settings.worker_max_atividades))
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=max_atividades, thread_name_prefix="cflow-no")
        )
        # Vagas de activity da fila de execução podem passar das threads: o excedente é de nó que
        # só espera rede (publicação no GCS, uma corrotina). O trabalho de CPU continua limitado
        # pelas threads acima e pelo teto de instâncias DuckDB (`instancia.entrar`), os dois em
        # `worker_max_atividades`. Sem `CFLOW_WORKER_VAGAS_ATIVIDADES`, vagas = threads, como antes.
        vagas_execucao = max(
            max_atividades, int(getattr(settings, "worker_vagas_atividades", None) or 0)
        )
        conexoes = int(settings.db_pool_size) + int(settings.db_max_overflow)
        if vagas_execucao > conexoes - 4:
            # Cada nó abre transações curtas; com mais vagas que conexões, o excedente espera
            # checkout do pool e pode estourar o `pool_timeout` no meio de uma corrida.
            logger.warning(
                "vagas_acima_do_pool",
                extra={"extra_campos": {"vagas": vagas_execucao, "conexoes_pool": conexoes}},
            )
        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[ExecutarPipeline, ExecutarDAG],
            activities=[executar_no_act, finalizar_act],
            max_concurrent_activities=vagas_execucao,
            graceful_shutdown_timeout=drenagem,
        )

        # Segundo worker, fila PRÓPRIA da ingestão: um backfill de dez anos são
        # centenas de activities; na fila compartilhada elas atrasariam a execução dos relatórios.
        worker_ingestao = Worker(
            client,
            task_queue=settings.ingestao_task_queue,
            workflows=[IngerirFonte],
            activities=[ingerir_fatia_act, concluir_ingestao_act, falhar_ingestao_act],
            max_concurrent_activities=max_atividades,
            graceful_shutdown_timeout=drenagem,
        )

        # Terceiro worker, fila própria dos RELATÓRIOS : um arquivo = uma activity, N em
        # paralelo. Uma base de um milhão de linhas leva minutos para virar xlsx e não pode ocupar o
        # slot da execução de amanhã nem o da ingestão.
        worker_relatorios = Worker(
            client,
            task_queue=settings.relatorios_task_queue,
            workflows=[GerarRelatorio],
            activities=[gerar_relatorio_act],
            max_concurrent_activities=max(1, settings.relatorios_concorrencia),
            graceful_shutdown_timeout=drenagem,
        )

        # Agendador: dispara os pipelines agendados (devidos) como workflows do Temporal.
        #
        # ENCADEADO : o disparo resolve o fecho de dependências e manda `ExecutarDAG` com
        # o mapa de frescor — o pipeline agendado lê o snapshot produzido NAQUELA corrida, igual
        # ao disparado pela tela. Antes disto o agendamento mandava `ExecutarPipeline` puro e a
        # fonte tipo `pipeline` caía em "a última execução concluída, qualquer uma": duas semânticas
        # para a mesma operação, e a errada era a automática. Pipeline SEM dependência degenera no
        # comportamento de sempre (é o caso de todos os agendamentos que existem hoje). O disparo
        # mora em `cflow_core.execucao.disparo` para ser o MESMO da rota "Executar agora".
        from cflow_core.execucao import disparo

        async def _iniciar_workflow(nome: str, args: list, workflow_id: str) -> None:
            await client.start_workflow(nome, args=args, id=workflow_id, task_queue=task_queue)

        _dispatch = disparo.montar_dispatch(_iniciar_workflow)

        # Cadência das fontes externas: as devidas viram execuções incrementais (do watermark
        # até hoje) — `fontes_devidas` já reserva a próxima janela na mesma transação, dois ticks
        # não disparam a mesma fonte duas vezes. Reaper de execução órfã (linha `executando` cujo
        # workflow o Temporal já não conhece): roda em toda réplica, a trava consultiva no Postgres
        # faz só uma delas consultar o orquestrador. Reaper do pool de fonte: fecha conexões
        # ociosas expiradas (só com CFLOW_POOL_FONTES ligado).
        from cflow_core.conexoes import pool
        from cflow_core.execucao import reconciliacao
        from cflow_core.ingestao import service as ingestao

        lacos: list[asyncio.Task] = []
        if not settings.worker_ticks:
            # Deployment só de activities (réplicas escaladas pelo KEDA): os laços cadenciados ficam
            # numa réplica só, ou na de sempre. O SKIP LOCKED/advisory lock já tornam N réplicas
            # corretas; a flag só evita N polls por tick.
            logger.info("worker_sem_ticks")
        else:
            lacos += [
                asyncio.create_task(
                    _repetir(lambda: tick_agendador(_dispatch), INTERVALO_AGENDADOR, parar)
                ),
                asyncio.create_task(
                    _repetir(
                        lambda: tick_ingestao(ingestao.executar, ingestao.fontes_devidas),
                        INTERVALO_INGESTAO,
                        parar,
                    )
                ),
                asyncio.create_task(
                    _repetir(
                        lambda: tick_reconciliacao(reconciliacao.reconciliar_com_trava),
                        INTERVALO_RECONCILIACAO,
                        parar,
                    )
                ),
            ]
        reaper = asyncio.create_task(pool.reaper_loop()) if settings.pool_fontes else None
        logger.info(
            "worker_pronto",
            extra={
                "extra_campos": {
                    "task_queue": task_queue,
                    "fila_ingestao": settings.ingestao_task_queue,
                    "fila_relatorios": settings.relatorios_task_queue,
                    "max_atividades": max_atividades,
                    "drenagem_segundos": proc.drenagem_segundos,
                }
            },
        )
        try:
            return await rodar_ate_parar(
                [worker, worker_ingestao, worker_relatorios],
                parar,
                estado,
                client.service_client.check_health,
                lacos,
                proc.drenagem_segundos,
            )
        finally:
            if reaper is not None:
                reaper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reaper
            # Fecha pools de fonte ociosos no encerramento do worker.
            pool.fechar_sqlserver()
            await pool.fechar_postgres()
    finally:
        if servidor is not None:
            servidor.close()
            await servidor.wait_closed()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
