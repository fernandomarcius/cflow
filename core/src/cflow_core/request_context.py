"""request_id context: contextvar + helpers. The HTTP middleware lives in the API package,
but the context is shared (api and worker correlate logs and audit through the same id).
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

CABECALHO_REQUEST_ID = "X-Request-Id"


def novo_request_id() -> str:
    """Generates a new correlation identifier."""
    return str(uuid.uuid4())


def definir_request_id(valor: str | None) -> str:
    """Sets the current context request_id (generates one if absent) and returns it."""
    valor = valor or novo_request_id()
    _request_id.set(valor)
    return valor


def request_id_atual() -> str | None:
    """The current context request_id, if any."""
    return _request_id.get()
