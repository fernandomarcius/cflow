"""Workflows de execução: um pipeline (grafo de nós) e uma CADEIA de pipelines (DAG).

Determinísticos — só decidem a ordem e chamam activities/child-workflows; todo IO (DuckDB, Postgres,
fontes) é nas activities. As execuções já foram criadas pela API (status 'executando'); aqui
rodamos os nós e finalizamos. Falha de nó → finaliza como 'falha' e propaga.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from cflow_core.execucao.paralelo import (
        MAX_NOS_EM_PARALELO,
        MAX_NOS_IO_EM_PARALELO,
        declara_grupo_serial,
        dependencias_dos_nos,
        rodar_por_dependencia,
        so_publica,
    )

    from .activities import HEARTBEAT_TIMEOUT, executar_no_act, finalizar_act

TASK_QUEUE = "cflow-execucao"

# Marcas de versão (`workflow.patched`) das mudanças de agenda. Um workflow iniciado ANTES
# delas e reexecutado (replay) por um worker novo — worker reiniciado no meio de uma corrida —
# segue o laço de antes: trocar a ordem das activities de um histórico já gravado quebra o
# determinismo e o Temporal falha a tarefa. Iniciado depois, ele grava a marca e segue a agenda
# por dependência (`cflow_core.execucao.paralelo`).
PATCH_NOS_POR_DEPENDENCIA = "nos-por-dependencia"
PATCH_CADEIA_POR_DEPENDENCIA = "cadeia-por-dependencia"
# Corrente serial por grupo (`config.grupo_serial`). A marca só é CONSULTADA quando a definição
# declara algum grupo — `workflow.patched` grava a marca no histórico ao ser chamada, e gravá-la
# em todo pipeline faria um rollback do worker falhar o replay de execuções que nem usam grupo.
# Pipeline sem grupo é byte-idêntico ao laço anterior e não toca nesta marca.
PATCH_GRUPO_SERIAL = "grupo-serial"
# Teto PRÓPRIO para a saída que só publica no lake (`paralelo.MAX_NOS_IO_EM_PARALELO`): a espera
# dela é de rede (GCS), não de CPU. Mesma disciplina da marca de grupo: só é consultada quando a
# definição tem alguma saída `lake`, então pipeline sem publicação segue byte-idêntico e um
# rollback do worker não quebra o replay dele. Iniciado antes da marca, o replay divide o teto de
# CPU com os demais nós, como sempre.
PATCH_TETO_IO = "teto-io-lake"

# Nós de EFEITO : escrevem fora da plataforma (banco externo / API / e-mail). A activity
# pode ser re-entregue após o commit externo (worker morre pré-ack); o engine já tem guarda de
# idempotência + fail-safe de re-entrada, mas capar a retentativa em 1 evita disparar de novo um
# efeito não idempotente (append/sql/http POST/e-mail enviado) e duplicar o efeito.
TIPOS_EFEITO = ("escrita_sql", "http", "email")


# Erro de DOMÍNIO (parâmetro inválido, snapshot ausente, SQL que não compila, guard de vazio):
# determinístico — a segunda tentativa só dobra o tempo até a falha aparecer na tela. O que vale
# retentar é infra (banco/lake fora, pod morto no meio), que chega como outra exceção.
NAO_RETENTAVEIS = ("ErroAplicacao",)


def politica_para(tipo: str | None) -> RetryPolicy:
    """RetryPolicy por tipo de nó: nós de efeito NÃO retentam (attempts=1); os demais retentam pouco
    (attempts=2 — erro de SQL/fonte é determinístico, não adianta insistir muito)."""
    return RetryPolicy(
        maximum_attempts=1 if tipo in TIPOS_EFEITO else 2,
        non_retryable_error_types=list(NAO_RETENTAVEIS),
    )


def args_do_no(
    execucao_id: str,
    definicao: dict,
    no_id: str,
    parametros: dict | None,
    fontes_execucao: dict | None,
    forcar: bool,
    sem_publicar: bool,
) -> list:
    """Argumentos de `executar_no_act`. `sem_publicar` só entra quando LIGADO: a lista do caminho
    de sempre fica byte-idêntica à que os workflows já gravados no histórico têm."""
    args = [execucao_id, definicao, no_id, parametros, fontes_execucao, forcar]
    return [*args, True] if sem_publicar else args


def args_do_pipeline(item: dict, fontes_execucao: dict, parametros: dict | None) -> list:
    """Argumentos do child `ExecutarPipeline` para um item da cadeia.

    Cada pipeline usa os parâmetros JÁ NORMALIZADOS para ELE (a API recortou aos que cada um declara
    e expandiu `competencia`→_inicio/_fim etc.). Não repassar o dict cru comum: um upstream pode
    declarar menos parâmetros e a raiz precisa dos derivados expandidos.

    `sem_publicar` (montante de raiz com `montante_publica: false`) vai POSICIONAL depois de
    `forcar_no`, e só quando ligado — mesmo motivo de `args_do_no`."""
    args = [
        item["execucao_id"],
        item["definicao"],
        item["ordem"],
        item.get("parametros", parametros),
        fontes_execucao,
    ]
    return [*args, None, True] if item.get("sem_publicar") else args


@workflow.defn
class ExecutarPipeline:
    @workflow.run
    async def run(
        self,
        execucao_id: str,
        definicao: dict,
        ordem: list[str],
        parametros: dict | None = None,
        fontes_execucao: dict | None = None,
        forcar_no: str | None = None,
        sem_publicar: bool = False,
    ) -> str:
        # `ordem` pode ser PARCIAL (reexecução a partir de um nó): o montante já entrou como
        # concluído/reaproveitado antes do disparo. `forcar_no` é o nó de partida, e só ele ignora
        # o reuso — os descendentes recalculam sozinhos se o conteúdo dele mudar.
        #
        # `sem_publicar` (último, default False — o workflow em voo que não o conhece continua
        # válido): montante de uma raiz com `montante_publica: false`. Os nós rodam e gravam
        # snapshot; saída não publica e o relatório padrão não é gerado.
        tipos = {n["id"]: n.get("tipo") for n in definicao.get("nos", [])}

        async def _no(no_id: str) -> None:
            await workflow.execute_activity(
                executar_no_act,
                args=args_do_no(
                    execucao_id,
                    definicao,
                    no_id,
                    parametros,
                    fontes_execucao,
                    no_id == forcar_no,
                    sem_publicar,
                ),
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=HEARTBEAT_TIMEOUT,
                retry_policy=politica_para(tipos.get(no_id)),
            )

        try:
            if workflow.patched(PATCH_NOS_POR_DEPENDENCIA):
                # Cada nó começa quando os predecessores DELE concluíram: as fontes independentes
                # de uma WIRE extraem ao mesmo tempo e os `gold_*` (folhas) publicam juntos. Nó de
                # efeito continua um por vez, na ordem de sempre (`paralelo.TIPOS_SERIAIS`) — salvo
                # os que declaram `grupo_serial`, que formam filas independentes. A definição é
                # entrada do workflow, então `declara_grupo_serial` é determinística no replay.
                grupos = declara_grupo_serial(definicao) and workflow.patched(PATCH_GRUPO_SERIAL)
                nos = {n.get("id"): n for n in definicao.get("nos", [])}
                io_proprio = any(so_publica(n) for n in nos.values()) and workflow.patched(
                    PATCH_TETO_IO
                )
                await rodar_por_dependencia(
                    ordem,
                    dependencias_dos_nos(definicao, ordem, grupos=grupos),
                    _no,
                    limite=MAX_NOS_EM_PARALELO,
                    io=(lambda nid: so_publica(nos.get(nid) or {})) if io_proprio else None,
                    limite_io=MAX_NOS_IO_EM_PARALELO if io_proprio else None,
                )
            else:
                for no_id in ordem:
                    await _no(no_id)
            await workflow.execute_activity(
                finalizar_act,
                args=[execucao_id, None, False, True] if sem_publicar else [execucao_id, None],
                start_to_close_timeout=timedelta(seconds=30),
            )
            return "concluida"
        except Exception:
            await workflow.execute_activity(
                finalizar_act,
                args=[execucao_id, "Falha na execução de um nó."],
                start_to_close_timeout=timedelta(seconds=30),
            )
            raise


@workflow.defn
class ExecutarDAG:
    """Encadeamento A→B: roda os pipelines da cadeia em ORDEM DE NÍVEL (dependências primeiro)
    como child workflows ExecutarPipeline — pipelines do MESMO nível (irmãos, sem dependência
    entre si) rodam em PARALELO (`asyncio.gather`, padrão do SDK p/ child workflows concorrentes);
    só espera o nível inteiro terminar antes de avançar pro próximo. `fontes_execucao`
    ({pipeline_id → execucao_id}) é montado pela API com TODAS as execuções pré-criadas → a raiz lê
    o snapshot EXATO de cada upstream rodado agora. Falha de um pipeline aborta a cadeia (os de
    cima — que dependem dele — não rodam) e as execuções que não chegaram a rodar são finalizadas
    como falha: pré-criadas, ficariam 'executando' para sempre e a tela mostraria uma cadeia em
    andamento que ninguém está executando.

    Com `deps` no payload, o pipeline não espera mais o nível inteiro — só os que ele lê
    (`cflow_core.execucao.paralelo`); a regra de falha é a mesma."""

    @workflow.run
    async def run(
        self, cadeia: list[dict], fontes_execucao: dict, parametros: dict | None = None
    ) -> str:
        niveis: dict[int, list[dict]] = {}
        for item in cadeia:
            niveis.setdefault(item.get("nivel", 0), []).append(item)

        async def _rodar(item: dict) -> None:
            await workflow.execute_child_workflow(
                ExecutarPipeline.run,
                args=args_do_pipeline(item, fontes_execucao, parametros),
                id=f"exec-{item['execucao_id']}",
            )

        # Por DEPENDÊNCIA quando o payload traz `deps` (API desta versão em diante): cada pipeline
        # começa quando os que ele lê terminaram. Payload sem `deps` — cadeia montada por uma API
        # anterior — cai na barreira por nível de sempre.
        por_dependencia = workflow.patched(PATCH_CADEIA_POR_DEPENDENCIA) and all(
            "deps" in item for item in cadeia
        )
        try:
            if por_dependencia:
                em_nivel = sorted(cadeia, key=lambda it: it.get("nivel", 0))
                por_pid = {item["pipeline_id"]: item for item in em_nivel}
                await rodar_por_dependencia(
                    list(por_pid),
                    {pid: item["deps"] for pid, item in por_pid.items()},
                    lambda pid: _rodar(por_pid[pid]),
                )
            else:
                for n in sorted(niveis):
                    # `return_exceptions=True` porque `gather` puro devolve o controle no
                    # PRIMEIRO erro e o irmão AINDA EM EXECUÇÃO fica sem ninguém para aguardá-lo —
                    # ao fechar, o pai termina o child workflow e a execução dele fica 'executando'
                    # para sempre. Espera o nível inteiro assentar e só então propaga: a cadeia
                    # falha igual, sem deixar preso.
                    falhas = [
                        r
                        for r in await asyncio.gather(
                            *(_rodar(item) for item in niveis[n]), return_exceptions=True
                        )
                        if isinstance(r, BaseException)
                    ]
                    if falhas:
                        raise falhas[0]
        except Exception:
            # A cadeia inteira falha. As execuções dos níveis SEGUINTES já foram criadas (o mapa de
            # frescor precisa estar completo antes de qualquer nó rodar) e nunca vão rodar: sem
            # isto ficariam 'executando' PARA SEMPRE, e a tela mostraria uma cadeia em andamento
            # que ninguém está executando.
            #
            # A varredura é sobre a cadeia INTEIRA, e não sobre uma lista do que "já começou":
            # execução cujo workflow filho falhou ao INICIAR não entraria nessa lista e é justamente
            # a que fica presa. O que torna a varredura larga segura é o `somente_se_executando` —
            # `finalizar` é um UPDATE cego, e sem o guard ele reescreveria como falha um irmão que
            # já CONCLUIU, trocando um snapshot bom por um desfecho mentiroso na tela e na trilha.
            for item in cadeia:
                await workflow.execute_activity(
                    finalizar_act,
                    args=[
                        item["execucao_id"],
                        "Cadeia interrompida: um pipeline do qual este depende falhou.",
                        True,
                    ],
                    start_to_close_timeout=timedelta(seconds=30),
                )
            raise
        return "concluida"
