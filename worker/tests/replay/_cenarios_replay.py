"""Cenários do teste de replay: as entradas e as activities de mentira que geram os históricos.

Cada cenário roda um workflow DE VERDADE (`cflow_worker.*_workflow`) num Temporal de teste com
relógio que salta (`WorkflowEnvironment.start_time_skipping`), com activities falsas que devolvem
sempre o mesmo resultado para a mesma entrada. O histórico que o servidor grava é o que o
`Replayer` reexecuta depois contra o código do PR: comando diferente no mesmo ponto do histórico =
não-determinismo = o worker novo falharia as execuções em voo no deploy (ver `workflow.patched`).

As activities falsas têm o MESMO nome das reais (`@activity.defn(name=...)`): o Replayer não as
executa (o resultado já está no histórico), mas o servidor de teste precisa de quem as atenda na
geração. Nada aqui toca banco, rede de produção ou o `cflow_core` além das funções puras de agenda.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from temporalio import activity
from temporalio.exceptions import ApplicationError

from cflow_worker.ingestao_workflow import IngerirFonte
from cflow_worker.relatorios_workflow import GerarRelatorio
from cflow_worker.workflow import ExecutarDAG, ExecutarPipeline

WORKFLOWS = [ExecutarPipeline, ExecutarDAG, IngerirFonte, GerarRelatorio]
FILA = "cflow-replay"

# Nó, fatia ou exportação com este marcador falha (erro de domínio, sem retentativa): é como os
# cenários de falha exercitam o caminho do `except` de cada workflow.
FALHA = "quebra"


@activity.defn(name="executar_no_act")
async def executar_no_falso(
    execucao_id: str,
    definicao: dict,
    no_id: str,
    parametros: dict | None = None,
    fontes_execucao: dict | None = None,
    forcar: bool = False,
    sem_publicar: bool = False,
) -> dict:
    if FALHA in no_id:
        raise ApplicationError(f"nó {no_id} falhou", type="ErroAplicacao", non_retryable=True)
    return {"no_id": no_id, "linhas": len(no_id), "reaproveitado": not forcar}


@activity.defn(name="finalizar_act")
async def finalizar_falso(
    execucao_id: str,
    erro: str | None,
    somente_se_executando: bool = False,
    sem_publicar: bool = False,
) -> None:
    return None


@activity.defn(name="ingerir_fatia_act")
async def ingerir_fatia_falso(
    fonte_id: str, execucao_id: str, fatia: dict, replay: bool = False
) -> dict:
    if fatia.get("id") == FALHA:
        raise ApplicationError("fatia recusada pela origem", non_retryable=True)
    return {"inseridas": int(fatia.get("linhas", 0)), "repetidas": 1 if replay else 0}


@activity.defn(name="concluir_ingestao_act")
async def concluir_ingestao_falso(
    fonte_id: str, execucao_id: str, contagens: dict, fatias: list[dict]
) -> None:
    return None


@activity.defn(name="falhar_ingestao_act")
async def falhar_ingestao_falso(execucao_id: str, erro: str, contagens: dict) -> None:
    return None


@activity.defn(name="gerar_relatorio_act")
async def gerar_relatorio_falso(exportacao_id: str) -> dict:
    return {"estado": "falha" if FALHA in exportacao_id else "pronto"}


ACTIVITIES = [
    executar_no_falso,
    finalizar_falso,
    ingerir_fatia_falso,
    concluir_ingestao_falso,
    falhar_ingestao_falso,
    gerar_relatorio_falso,
]


@dataclass(frozen=True)
class Cenario:
    nome: str
    workflow: str
    args: list = field(default_factory=list)
    falha: bool = False

    @property
    def workflow_id(self) -> str:
        return f"replay-{self.nome}"


def _uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def _no(no_id: str, tipo: str, **config) -> dict:
    return {"id": no_id, "tipo": tipo, "config": config}


# Pipeline típico: duas fontes independentes, uma transformação, uma saída que só publica no lake
# (teto de I/O próprio) e uma saída em arquivo (serial).
_DEF_PUBLICA = {
    "nos": [
        _no("fonte_a", "fonte"),
        _no("fonte_b", "fonte"),
        _no("juntar", "sql"),
        _no("gold_lake", "saida", destino="lake"),
        _no("arquivo", "saida", destino="arquivo"),
    ],
    "arestas": [
        {"de": "fonte_a", "para": "juntar"},
        {"de": "fonte_b", "para": "juntar"},
        {"de": "juntar", "para": "gold_lake"},
        {"de": "juntar", "para": "arquivo"},
    ],
}
_ORDEM_PUBLICA = ["fonte_a", "fonte_b", "juntar", "gold_lake", "arquivo"]

# Nós de efeito em correntes seriais por grupo (marca `grupo-serial`) e um e-mail sem grupo.
_DEF_GRUPOS = {
    "nos": [
        _no("base", "sql"),
        _no("grava_a1", "escrita_sql", grupo_serial="a"),
        _no("grava_b1", "escrita_sql", grupo_serial="b"),
        _no("grava_a2", "escrita_sql", grupo_serial="a"),
        _no("avisa", "email"),
    ],
    "arestas": [
        {"de": "base", "para": "grava_a1"},
        {"de": "base", "para": "grava_b1"},
        {"de": "grava_a1", "para": "grava_a2"},
    ],
}
_ORDEM_GRUPOS = ["base", "grava_a1", "grava_b1", "grava_a2", "avisa"]

# Sem saída `lake` e sem grupo: o caminho que NÃO consulta as marcas `teto-io-lake`/`grupo-serial`.
_DEF_SIMPLES = {
    "nos": [_no("fonte", "fonte"), _no("calculo", "sql"), _no("saida", "saida")],
    "arestas": [{"de": "fonte", "para": "calculo"}, {"de": "calculo", "para": "saida"}],
}
_ORDEM_SIMPLES = ["fonte", "calculo", "saida"]

_DEF_FALHA = {
    "nos": [_no("fonte", "fonte"), _no("quebra_sql", "sql"), _no("saida", "saida")],
    "arestas": [{"de": "fonte", "para": "quebra_sql"}, {"de": "quebra_sql", "para": "saida"}],
}
_ORDEM_FALHA = ["fonte", "quebra_sql", "saida"]

_PARAMETROS = {"competencia": "2026-09", "competencia_inicio": "2026-09-01"}


def _item(n: int, pipeline: str, nivel: int, definicao: dict, ordem: list, deps=None, **extra):
    item = {
        "execucao_id": _uuid(n),
        "pipeline_id": pipeline,
        "nivel": nivel,
        "definicao": definicao,
        "ordem": ordem,
        "parametros": _PARAMETROS,
        **extra,
    }
    if deps is not None:
        item["deps"] = deps
    return item


_FONTES = {"crc": _uuid(10), "wire": _uuid(11), "ref": _uuid(12)}

CENARIOS = [
    Cenario(
        "pipeline-publica-lake",
        "ExecutarPipeline",
        [_uuid(1), _DEF_PUBLICA, _ORDEM_PUBLICA, _PARAMETROS, {"crc": _uuid(10)}],
    ),
    Cenario("pipeline-grupos-serial", "ExecutarPipeline", [_uuid(2), _DEF_GRUPOS, _ORDEM_GRUPOS]),
    Cenario(
        "pipeline-reexecucao-sem-publicar",
        "ExecutarPipeline",
        [_uuid(3), _DEF_SIMPLES, ["calculo", "saida"], _PARAMETROS, None, "calculo", True],
    ),
    Cenario(
        "pipeline-falha-no",
        "ExecutarPipeline",
        [_uuid(4), _DEF_FALHA, _ORDEM_FALHA, _PARAMETROS],
        falha=True,
    ),
    Cenario(
        "cadeia-por-dependencia",
        "ExecutarDAG",
        [
            [
                _item(10, "crc", 0, _DEF_SIMPLES, _ORDEM_SIMPLES, deps=[]),
                _item(11, "wire", 1, _DEF_SIMPLES, _ORDEM_SIMPLES, deps=["crc"]),
                _item(12, "ref", 1, _DEF_PUBLICA, _ORDEM_PUBLICA, deps=["crc"], sem_publicar=True),
            ],
            _FONTES,
            _PARAMETROS,
        ],
    ),
    Cenario(
        "cadeia-por-nivel",
        "ExecutarDAG",
        [
            [
                _item(20, "crc", 0, _DEF_SIMPLES, _ORDEM_SIMPLES),
                _item(21, "wire", 1, _DEF_SIMPLES, _ORDEM_SIMPLES),
                _item(22, "ref", 1, _DEF_SIMPLES, _ORDEM_SIMPLES),
            ],
            {"crc": _uuid(20), "wire": _uuid(21), "ref": _uuid(22)},
        ],
    ),
    Cenario(
        "cadeia-falha",
        "ExecutarDAG",
        [
            [
                _item(30, "crc", 0, _DEF_FALHA, _ORDEM_FALHA, deps=[]),
                _item(31, "wire", 1, _DEF_SIMPLES, _ORDEM_SIMPLES, deps=["crc"]),
            ],
            {"crc": _uuid(30), "wire": _uuid(31)},
        ],
        falha=True,
    ),
    Cenario(
        "ingestao-lotes",
        "IngerirFonte",
        [
            "fonte-sgs",
            _uuid(40),
            [{"id": f"2026-0{m}", "linhas": 10 * m} for m in range(1, 6)],
            False,
            2,
        ],
    ),
    Cenario(
        "ingestao-falha-fatia",
        "IngerirFonte",
        ["fonte-sgs", _uuid(41), [{"id": "2026-01", "linhas": 3}, {"id": FALHA}], True, 4],
        falha=True,
    ),
    Cenario("relatorio-pronto", "GerarRelatorio", [_uuid(50)]),
    Cenario("relatorio-falha-dado", "GerarRelatorio", [f"{FALHA}-{_uuid(51)}"]),
]
