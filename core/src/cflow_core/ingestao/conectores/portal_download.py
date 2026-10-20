"""Class `portal_download`: a public portal without a stable URL contract.

Fetches the index page, extracts the links matching `padrao_link` and downloads each one like
`http_file`. It is the least predictable class - so it has a file cap, and each extracted link is
resolved against the base and **restricted to the index page host**: a compromised portal cannot
make ingestion download from anywhere on the internet.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from ..contrato import Fatia, resolver
from .base import Payload, Requisicao, corpo_ou_erro, nome_da_url, requisitar

_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


class ConectorPortalDownload:
    classe = "portal_download"

    async def descobrir(self, contrato: dict, fatia: Fatia) -> list[Requisicao]:
        acesso = contrato.get("acesso") or {}
        variaveis = acesso.get("variaveis")
        indice = resolver(acesso["pagina_indice"], fatia, variaveis)
        padrao = re.compile(acesso["padrao_link"], re.IGNORECASE)
        maximo = int(acesso.get("max_arquivos", 20))
        host_indice = urlparse(indice).netloc

        r = await requisitar(
            contrato,
            Requisicao(fatia=fatia, url=indice, nome_arquivo="indice.html", condicional=False),
        )
        html = corpo_ou_erro(r, url=indice).decode("utf-8", errors="replace")

        vistos: set[str] = set()
        reqs: list[Requisicao] = []
        for href in _HREF.findall(html):
            if not padrao.search(href):
                continue
            url = urljoin(indice, href)
            if urlparse(url).scheme != "https" or urlparse(url).netloc != host_indice:
                continue  # outside the portal host: not our source
            if url in vistos:
                continue
            vistos.add(url)
            reqs.append(
                Requisicao(
                    fatia=fatia,
                    url=url,
                    nome_arquivo=nome_da_url(url, padrao=f"{contrato['dataset']}_{len(reqs)}.bin"),
                    headers=dict(acesso.get("headers") or {}),
                    condicional=bool(acesso.get("condicional", True)),
                )
            )
            if len(reqs) >= maximo:
                break
        return reqs

    async def baixar(
        self,
        contrato: dict,
        req: Requisicao,
        *,
        etag: str | None = None,
        ultima_modificacao: str | None = None,
    ) -> Payload:
        r = await requisitar(contrato, req, etag=etag, ultima_modificacao=ultima_modificacao)
        if r.status_code == 404:
            return Payload(dados=b"", origem_url=req.url, nao_publicado=True)
        if r.status_code == 304:
            return Payload(
                dados=b"",
                origem_url=req.url,
                nao_modificado=True,
                etag=etag,
                ultima_modificacao=ultima_modificacao,
            )
        return Payload(
            dados=corpo_ou_erro(r, url=req.url),
            origem_url=req.url,
            etag=r.headers.get("ETag"),
            ultima_modificacao=r.headers.get("Last-Modified"),
            tipo_mime=r.headers.get("Content-Type", "application/octet-stream").split(";")[0],
        )
