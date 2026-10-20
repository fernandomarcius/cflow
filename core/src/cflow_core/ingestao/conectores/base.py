"""Ingestion connector contract: two verbs, no source known by name.

`descobrir(contrato, fatia)` says WHICH bytes to fetch; `baixar(...)` brings the bytes. The
connector does not know which provider it is talking to - the contract does. That is what makes
a new source a registration, not a module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from ...config import get_settings
from ...errors import ErroAplicacao
from ...retry import espera
from ..contrato import Fatia


@dataclass(frozen=True)
class Requisicao:
    """What to fetch for a slice. A slice may yield N requests (a portal with N files)."""

    fatia: Fatia
    url: str
    nome_arquivo: str
    metodo: str = "GET"
    params: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    condicional: bool = True
    # Encodes EVERY reserved character in the query, including the single quote. Some OData services
    # reject `$filter=Date ge '2026-07-01'` with HTTP 400 and accept the SAME query with `%27` - and
    # httpx, correct per RFC 3986, sends the raw quote (a valid sub-delim). Without this, such a
    # service is never ingested and the error that arrives is an unexplained 400.
    codificar_estrito: bool = False


@dataclass(frozen=True)
class Payload:
    """The RAW bytes, exactly as they arrived. The hash and landing belong to the raw payload,
    before any decoding - it is what proves what the origin delivered."""

    dados: bytes
    origem_url: str
    etag: str | None = None
    ultima_modificacao: str | None = None
    tipo_mime: str = "application/octet-stream"
    nao_modificado: bool = False
    # The origin did not publish this slice (404). NOT a failure: a daily-granularity source has no
    # file on weekends, holidays or future periods. Treating that as quarantine drowns the DLQ -
    # measured on a production-like host, most "failures" were just non-business days, and then
    # nobody looks at the DLQ any more, which is where the real problem lives.
    nao_publicado: bool = False
    detalhe: dict = field(default_factory=dict)


class ConectorIngestao(Protocol):
    classe: str

    async def descobrir(self, contrato: dict, fatia: Fatia) -> list[Requisicao]: ...

    async def baixar(
        self,
        contrato: dict,
        req: Requisicao,
        *,
        etag: str | None = None,
        ultima_modificacao: str | None = None,
    ) -> Payload: ...


# --- HTTP shared by all classes ------------------------------------------------------------


def causa_http(exc: Exception) -> str:
    """Sanitized cause - an ingestion error message goes to the audit trail and to the screen; a
    URL with a query token inside must not leak that way."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP response {exc.response.status_code}"
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "host unreachable"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    return "failed to access the source"


def _timeout(contrato: dict) -> int:
    from ..contrato import limite

    return limite(contrato, "timeout_segundos", get_settings().ingestao_http_timeout_segundos)


def _tentativas(contrato: dict) -> int:
    from ..contrato import limite

    return limite(contrato, "tentativas", 3)


async def requisitar(
    contrato: dict,
    req: Requisicao,
    *,
    etag: str | None = None,
    ultima_modificacao: str | None = None,
    params_extra: dict | None = None,
):
    """GET with short retry and backoff. Returns the `httpx.Response` (the caller decides what to
    do with 304/4xx).

    Retries only TRANSPORT failures and 5xx/429: a 404 does not improve by insisting - it means
    the slice has not been published yet, and hammering the origin for that is how you get blocked."""
    import httpx

    cabecalhos = dict(req.headers or {})
    if req.condicional:
        # Conditional on the ETag/Last-Modified of the last batch of THIS slice: it avoids
        # re-downloading a large file every hour just to find out nothing changed.
        if etag:
            cabecalhos["If-None-Match"] = etag
        if ultima_modificacao:
            cabecalhos["If-Modified-Since"] = ultima_modificacao

    params = {**(req.params or {}), **(params_extra or {})}
    url = req.url
    if params and req.codificar_estrito:
        from urllib.parse import quote, urlencode

        url = f"{req.url}?{urlencode(params, quote_via=quote, safe='')}"
        params = {}
    tentativas = _tentativas(contrato)
    ultimo: Exception | None = None
    async with httpx.AsyncClient(timeout=_timeout(contrato), follow_redirects=True) as cli:
        for tentativa in range(tentativas):
            try:
                r = await cli.request(
                    req.metodo, url, params=params or None, headers=cabecalhos or None
                )
            except httpx.HTTPError as exc:
                ultimo = exc
            else:
                if r.status_code < 500 and r.status_code != 429:
                    return r
                ultimo = httpx.HTTPStatusError("transient error", request=r.request, response=r)
            if tentativa < tentativas - 1:
                # Same formula as always, now from the shared module.
                await asyncio.sleep(espera(tentativa))
    raise ErroAplicacao(
        codigo="falha_download",
        mensagem="Failed to download from the source: "
        + (causa_http(ultimo) if ultimo else "unknown error")
        + ".",
        status=502,
    )


def corpo_ou_erro(r, *, url: str) -> bytes:
    """Extracts the bytes checking status and size cap. The cap exists because the payload is read
    ENTIRELY into memory to hash before writing - without a cap, an unexpected multi-GB file takes
    the worker down instead of becoming quarantine."""
    if r.status_code >= 400:
        raise ErroAplicacao(
            codigo="falha_download",
            mensagem=f"The source responded with HTTP {r.status_code}.",
            status=502,
        )
    dados = r.content
    teto = get_settings().ingestao_download_max_mb * 1024 * 1024
    if len(dados) > teto:
        raise ErroAplicacao(
            codigo="download_grande_demais",
            mensagem=f"Download exceeds the cap of {get_settings().ingestao_download_max_mb} MB.",
            status=413,
        )
    if not dados:
        raise ErroAplicacao(
            codigo="download_vazio",
            mensagem=f"The source returned an empty body ({url}).",
            status=502,
        )
    return dados


def nome_da_url(url: str, *, padrao: str) -> str:
    """File name in landing. Falls back to `padrao` when the URL has no usable basename (API
    endpoint); never accepts `/` or `..` (the name comes from a third party)."""
    from urllib.parse import urlparse

    base = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    if not base or base in (".", "..") or "\\" in base:
        return padrao
    return base
