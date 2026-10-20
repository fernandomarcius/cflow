"""Arnês dos testes do worker.

Estes testes NÃO tocam banco, rede nem Temporal — os dois `tick` recebem por injeção o que
faria isso. O guard abaixo torna essa afirmação verificável em vez de documentada: se um dia
alguém trouxer `transacao()` para dentro de um tick, o teste falha com a razão na mensagem, e não
com um `ConnectionRefused` que parece problema de ambiente.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def sem_banco(monkeypatch):
    def _proibido(*_a, **_kw):
        raise AssertionError(
            "tick do worker abriu transação com o banco: estes testes são de unidade e o acesso "
            "a dados entra por injeção (`executar`/`listar_devidas`/`dispatch`)."
        )

    monkeypatch.setattr("cflow_core.db.transacao", _proibido)
