"""Registry of ingestion connectors BY ACCESS CLASS.

Four classes cover the sources. A new source that fits a class is just a registration; one that
does not fit requires a new class - an explicit, recorded decision, not a hidden `if` in the
connector.
"""

from __future__ import annotations

from ...errors import ErroAplicacao
from .base import ConectorIngestao, Payload, Requisicao
from .http_file import ConectorHttpFile
from .odata import ConectorOdata
from .portal_download import ConectorPortalDownload
from .rest_api import ConectorRestApi

_REGISTRO: dict[str, ConectorIngestao] = {
    "http_file": ConectorHttpFile(),
    "rest_api": ConectorRestApi(),
    "odata": ConectorOdata(),
    "portal_download": ConectorPortalDownload(),
}


def get_conector(classe: str) -> ConectorIngestao:
    conector = _REGISTRO.get(classe)
    if conector is None:
        raise ErroAplicacao(
            codigo="classe_nao_suportada",
            mensagem=f"Access class '{classe}' is not supported by ingestion.",
            status=400,
        )
    return conector


__all__ = ["ConectorIngestao", "Payload", "Requisicao", "get_conector"]
