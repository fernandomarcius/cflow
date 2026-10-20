"""Execution context used to correlate worker logs with a pipeline execution.

Correlation by request_id already exists separately.
"""

from __future__ import annotations

from contextvars import ContextVar

_execucao_id: ContextVar[str | None] = ContextVar("execucao_id", default=None)


def definir_execucao_id(valor: str | None) -> None:
    _execucao_id.set(valor)


def execucao_id_atual() -> str | None:
    return _execucao_id.get()
