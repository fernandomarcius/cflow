"""Replay do Temporal: o código dos workflows DESTE commit reexecuta os históricos já gravados?

Um worker novo que sobe no meio de uma corrida reexecuta o histórico dela desde o começo. Se o
código mudou a sequência de comandos (activity a mais, ordem trocada, timer novo) sem
`workflow.patched`, o Temporal falha a tarefa por não-determinismo e a execução fica presa. Os
testes de unidade não veem isso; o `Replayer` vê, sem servidor, banco nem rede.

Históricos reexecutados:
- `tests/replay/historicos/*.json` — versionados, gerados por `gerar_historicos.py` com o código
  da época em que cada cenário entrou (não regravar para "fazer passar": ver o gerador);
- `$CFLOW_REPLAY_HISTORICOS_BASE/*.json`, quando definido — o pr-gate gera com o código da BASE do
  PR e reexecuta aqui com o do PR (nó `replay`).
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _cenarios_replay import CENARIOS, WORKFLOWS  # noqa: E402

VERSIONADOS = Path(__file__).resolve().parent / "historicos"


def _historicos() -> list[Path]:
    arquivos = sorted(VERSIONADOS.glob("*.json"))
    extra = os.environ.get("CFLOW_REPLAY_HISTORICOS_BASE")
    if extra:
        da_base = sorted(Path(extra).glob("*.json"))
        assert da_base, f"CFLOW_REPLAY_HISTORICOS_BASE={extra} sem nenhum histórico"
        arquivos += da_base
    return arquivos


def _tipo(arquivo: Path) -> str:
    inicio = json.loads(arquivo.read_text())["events"][0]
    return inicio["workflowExecutionStartedEventAttributes"]["workflowType"]["name"]


@pytest.mark.parametrize("arquivo", _historicos(), ids=lambda a: f"{a.parent.name}/{a.stem}")
async def test_replay_sem_nao_determinismo(arquivo: Path):
    historico = WorkflowHistory.from_json(arquivo.stem, arquivo.read_text())
    resultado = await Replayer(workflows=WORKFLOWS).replay_workflow(
        historico, raise_on_replay_failure=False
    )
    assert resultado.replay_failure is None, (
        f"{arquivo.name}: o código atual não reexecuta este histórico "
        f"({resultado.replay_failure!r}). Mudança de agenda em workflow exige `workflow.patched`."
    )


def test_todo_workflow_registrado_tem_historico():
    """Workflow novo registrado no worker sem cenário de replay seria um buraco silencioso."""
    # Lido do fonte (`Worker(..., workflows=[...])` em main.py): importar o main sobe a fiação.
    arvore = ast.parse(
        (Path(__file__).resolve().parents[2] / "src/cflow_worker/main.py").read_text()
    )
    registrados = {
        el.id
        for no in ast.walk(arvore)
        if isinstance(no, ast.keyword) and no.arg == "workflows" and isinstance(no.value, ast.List)
        for el in no.value.elts
        if isinstance(el, ast.Name)
    }
    assert registrados, "nenhum `workflows=[...]` achado em main.py"
    com_historico = {_tipo(a) for a in VERSIONADOS.glob("*.json")}
    assert registrados <= com_historico, f"sem histórico de replay: {registrados - com_historico}"
    assert {c.workflow for c in CENARIOS} == registrados


def test_cenarios_cobrem_sucesso_e_falha_de_cada_workflow():
    por_tipo: dict[str, set[bool]] = {}
    for c in CENARIOS:
        por_tipo.setdefault(c.workflow, set()).add(c.falha)
    # GerarRelatorio não falha por dado (quem decide é `service.gerar`): só o caminho feliz conta
    incompletos = {t for t, f in por_tipo.items() if f != {True, False} and t != "GerarRelatorio"}
    assert not incompletos, f"cenário de falha faltando para {incompletos}"
