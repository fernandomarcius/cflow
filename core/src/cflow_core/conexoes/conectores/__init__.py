"""Source connectors by type. Each type implements the same protocol (test, schema, sample),
isolating the driver. No type is hardcoded in the service layer.
"""

from __future__ import annotations

from ...errors import ErroAplicacao
from .api_query import ConectorApiQuery
from .api_rest import ConectorApiRest
from .arquivo import ConectorArquivo
from .base import AnexoEmail, Conector, ConectorEmail, ConectorEscrita, MensagemEmail
from .objeto import ConectorObjeto
from .postgres import ConectorPostgres
from .smtp import ConectorSmtp
from .sqlserver import ConectorSqlServer

_REGISTRO: dict[str, Conector] = {
    "postgres": ConectorPostgres(),
    "sqlserver": ConectorSqlServer(),
    "api_rest": ConectorApiRest(),
    "api_query": ConectorApiQuery(),
    "arquivo": ConectorArquivo(),
    "smtp": ConectorSmtp(),
    "objeto": ConectorObjeto(),
}

# Connection types that support the `escrita_sql` EFFECT node (write destination). The same registry
# object implements `ConectorEscrita` (see postgres.py / sqlserver.py).
TIPOS_ESCRITA_SQL = {"postgres", "sqlserver"}

# Types that support the `email` EFFECT node. The registry object implements `ConectorEmail`.
TIPOS_EMAIL = {"smtp"}


def get_conector(tipo: str) -> Conector:
    conector = _REGISTRO.get(tipo)
    if conector is None:
        raise ErroAplicacao(
            codigo="tipo_nao_suportado",
            mensagem=f"Connections of type '{tipo}' are not yet supported for testing/browsing.",
            status=400,
        )
    return conector


def get_conector_escrita(tipo: str) -> ConectorEscrita:
    """WRITE connector by type. 400 if the type does not support writes (postgres/sqlserver only)."""
    if tipo not in TIPOS_ESCRITA_SQL:
        raise ErroAplicacao(
            codigo="tipo_sem_escrita",
            mensagem=f"Connections of type '{tipo}' do not support writes (use postgres or sqlserver).",
            status=400,
        )
    return _REGISTRO[tipo]  # type: ignore[return-value]


def get_conector_email(tipo: str) -> ConectorEmail:
    """E-MAIL connector by type. 400 if the type cannot send e-mail (smtp only)."""
    if tipo not in TIPOS_EMAIL:
        raise ErroAplicacao(
            codigo="tipo_sem_email",
            mensagem=f"Connections of type '{tipo}' cannot send e-mail (use smtp).",
            status=400,
        )
    return _REGISTRO[tipo]  # type: ignore[return-value]


__all__ = [
    "AnexoEmail",
    "Conector",
    "ConectorEmail",
    "ConectorEscrita",
    "MensagemEmail",
    "TIPOS_EMAIL",
    "TIPOS_ESCRITA_SQL",
    "get_conector",
    "get_conector_email",
    "get_conector_escrita",
]
