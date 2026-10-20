"""Class `rest_api`: JSON paginated by date WINDOW.

Some APIs refuse windows beyond a maximum span, so incremental and backfill share the same
mechanics: the contract declares `passo_max_dias` and the slicing (`contrato.fatias`) already
delivers ready-made windows. Here we only build the slice request and keep the raw response bytes.
"""

from __future__ import annotations

import json

from ...errors import ErroAplicacao
from ..contrato import Fatia, resolver
from .base import Payload, Requisicao, corpo_ou_erro, requisitar


class ConectorRestApi:
    classe = "rest_api"

    async def descobrir(self, contrato: dict, fatia: Fatia) -> list[Requisicao]:
        acesso = contrato.get("acesso") or {}
        variaveis = acesso.get("variaveis")
        url = resolver(acesso["url_padrao"], fatia, variaveis)
        params = {
            k: resolver(str(v), fatia, variaveis) for k, v in (acesso.get("params") or {}).items()
        }
        fmt = acesso.get("formato_data", "%Y-%m-%d")
        if acesso.get("param_inicio"):
            params[acesso["param_inicio"]] = fatia.inicio.strftime(fmt)
        if acesso.get("param_fim"):
            params[acesso["param_fim"]] = fatia.fim.strftime(fmt)
        return [
            Requisicao(
                fatia=fatia,
                url=url,
                nome_arquivo=f"{contrato['dataset']}_{fatia.chave}.json",
                params=params,
                headers=dict(acesso.get("headers") or {}),
                condicional=bool(acesso.get("condicional", False)),
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
        if r.status_code == 304:
            return Payload(
                dados=b"",
                origem_url=req.url,
                nao_modificado=True,
                etag=etag,
                ultima_modificacao=ultima_modificacao,
            )
        dados = corpo_ou_erro(r, url=req.url)
        # Fail early, with a name: some APIs return 200 + an HTML error when the window is too
        # large. Without this check, an error page would become a "valid" batch in landing and an
        # empty bronze - the costliest failure mode, because it looks like success.
        try:
            json.loads(dados.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ErroAplicacao(
                codigo="resposta_nao_json",
                mensagem="The API responded 200 but the body is not valid JSON "
                "(window too large, or error returned as a page?).",
                status=502,
            ) from None
        return Payload(
            dados=dados,
            origem_url=str(r.request.url),
            etag=r.headers.get("ETag"),
            ultima_modificacao=r.headers.get("Last-Modified"),
            tipo_mime="application/json",
        )
