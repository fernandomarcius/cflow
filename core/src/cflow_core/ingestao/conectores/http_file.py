"""Class `http_file`: a file published under a URL convention.

Discovery is the convention itself: `report_{year}{month}.zip`, `daily_A{year}.ZIP`. Incremental
loading uses `If-None-Match`/`If-Modified-Since` - servers that answer ETag and Last-Modified let
a `304` cost one round trip instead of the whole file.
"""

from __future__ import annotations

from ..contrato import Fatia, resolver
from .base import Payload, Requisicao, corpo_ou_erro, nome_da_url, requisitar


class ConectorHttpFile:
    classe = "http_file"

    async def descobrir(self, contrato: dict, fatia: Fatia) -> list[Requisicao]:
        acesso = contrato.get("acesso") or {}
        url = resolver(acesso["url_padrao"], fatia, acesso.get("variaveis"))
        return [
            Requisicao(
                fatia=fatia,
                url=url,
                nome_arquivo=nome_da_url(url, padrao=f"{contrato['dataset']}_{fatia.chave}.bin"),
                params=dict(acesso.get("params") or {}),
                headers=dict(acesso.get("headers") or {}),
                condicional=bool(acesso.get("condicional", True)),
            )
        ]

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
            # Neither a failure nor a new batch: the slice is unchanged from what is already in landing.
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
