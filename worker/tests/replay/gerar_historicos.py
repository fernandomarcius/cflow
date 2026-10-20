"""Gera os históricos do teste de replay rodando cada cenário num Temporal de teste.

    uv run python tests/replay/gerar_historicos.py <pasta>        # ex.: tests/replay/historicos

Usa `WorkflowEnvironment.start_time_skipping()` (o SDK baixa o servidor de teste na primeira vez):
o relógio salta, então retentativa e timeout não esperam de verdade, e a mesma entrada produz a
mesma sequência de eventos. Grava um `<workflow_id>.json` por workflow (inclusive os filhos do
`ExecutarDAG`).

Quando gravar de novo os históricos versionados (`tests/replay/historicos/`): só ao ACRESCENTAR um
cenário ou workflow. Mudar um workflow e regravar para o replay passar apaga justamente a prova de
que o código novo ainda reexecuta o que já está em voo — use `workflow.patched`. O pr-gate também
gera os históricos com o código da BASE do PR e os reexecuta com o código do PR (nó `replay`).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from temporalio.client import WorkflowFailureError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _cenarios_replay import ACTIVITIES, CENARIOS, FILA, WORKFLOWS  # noqa: E402


def _anonimo(no):
    """Tira do histórico o que é da máquina que gerou (pid@host, caminho no stack trace): o
    Replayer não usa nenhum dos dois, e o arquivo versionado não deve carregar nome de máquina."""
    if isinstance(no, dict):
        return {
            k: ("gerador-replay" if k == "identity" else "" if k == "stackTrace" else _anonimo(v))
            for k, v in no.items()
        }
    if isinstance(no, list):
        return [_anonimo(v) for v in no]
    return no


async def gerar(destino: Path) -> int:
    destino.mkdir(parents=True, exist_ok=True)
    gravados = 0
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue=FILA, workflows=WORKFLOWS, activities=ACTIVITIES):
            for cenario in CENARIOS:
                handle = await env.client.start_workflow(
                    cenario.workflow, args=cenario.args, id=cenario.workflow_id, task_queue=FILA
                )
                try:
                    await handle.result()
                    falhou = False
                except WorkflowFailureError:
                    falhou = True
                if falhou != cenario.falha:
                    raise SystemExit(
                        f"{cenario.nome}: esperado {'falha' if cenario.falha else 'sucesso'}, "
                        f"veio {'falha' if falhou else 'sucesso'}"
                    )
                ids = [cenario.workflow_id]
                if cenario.workflow == "ExecutarDAG":
                    ids += [f"exec-{item['execucao_id']}" for item in cenario.args[0]]
                for wid in ids:
                    try:
                        historico = await env.client.get_workflow_handle(wid).fetch_history()
                    except RPCError as e:
                        # filho que a cadeia não chegou a iniciar (dependência falhou)
                        if e.status == RPCStatusCode.NOT_FOUND:
                            continue
                        raise
                    corpo = _anonimo(json.loads(historico.to_json()))
                    (destino / f"{wid}.json").write_text(json.dumps(corpo, indent=1) + "\n")
                    gravados += 1
    return gravados


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    n = asyncio.run(gerar(Path(sys.argv[1])))
    print(f"{n} históricos gravados em {sys.argv[1]}")
