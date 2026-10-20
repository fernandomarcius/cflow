"""Structured technical log (JSON), correlated by request_id.

Separate from the audit trail (which is permanent).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from .request_context import request_id_atual


class FormatadorJSON(logging.Formatter):
    """Formats each log record as a JSON line with request_id."""

    def format(self, record: logging.LogRecord) -> str:
        from .auditoria.execucao_context import execucao_id_atual

        dados = {
            "momento": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "nivel": record.levelname,
            "logger": record.name,
            "mensagem": record.getMessage(),
            "request_id": request_id_atual(),
            "execucao_id": execucao_id_atual(),
        }
        if record.exc_info:
            dados["excecao"] = self.formatException(record.exc_info)
        for chave, valor in getattr(record, "extra_campos", {}).items():
            dados[chave] = valor
        return json.dumps(dados, ensure_ascii=False)


def configurar_logging(nivel: str = "INFO") -> None:
    """Configures the root logger to emit JSON on stdout. Idempotent."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(FormatadorJSON())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(nivel.upper())
