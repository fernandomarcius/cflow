"""Class `odata`: `$top`/`$skip` pagination.

The whole slice becomes ONE payload: pages are concatenated into a single JSON before going to
landing. Storing page by page would make idempotency per page (the service merely reordering would
"change" the batch) and would turn one slice into N ownerless objects.
"""

from __future__ import annotations

import json

from ...errors import ErroAplicacao
from ..contrato import Fatia, resolver
from .base import Payload, Requisicao, corpo_ou_erro, requisitar


class ConectorOdata:
    classe = "odata"

    async def descobrir(self, contrato: dict, fatia: Fatia) -> list[Requisicao]:
        acesso = contrato.get("acesso") or {}
        variaveis = acesso.get("variaveis")
        url = resolver(acesso["url_padrao"], fatia, variaveis)

        def _resolver(mapa: dict) -> dict:
            return {k: resolver(str(v), fatia, variaveis) for k, v in (mapa or {}).items()}

        params = _resolver(acesso.get("params"))
        params.update(_resolver(acesso.get("params_janela")))
        params.setdefault("$format", "json")
        return [
            Requisicao(
                fatia=fatia,
                url=url,
                nome_arquivo=f"{contrato['dataset']}_{fatia.chave}.json",
                params=params,
                headers=dict(acesso.get("headers") or {}),
                condicional=False,  # pagination is already the incremental; conditional GET does not apply
                codificar_estrito=True,  # some services require `%27` for the $filter quote (400 otherwise)
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
        acesso = contrato.get("acesso") or {}
        top = int(acesso.get("top", 1000))
        campo = acesso.get("campo_itens", "value")
        # `paginar: false` - a service that does NOT accept `$skip`. Such services exist: some
        # return 500 for any `$skip`, even `$skip=0`. In that case ONE request is sent, and if the
        # page comes back FULL the batch is marked `truncado` (there is no way to know what was left
        # behind - and pretending everything arrived is the worse outcome).
        paginar = bool(acesso.get("paginar", True))
        max_paginas = int(acesso.get("max_paginas", 200)) if paginar else 1

        itens: list = []
        paginas = 0
        truncado = False
        for pagina in range(max_paginas):
            extra = {"$top": top}
            if paginar:
                extra["$skip"] = pagina * top
            r = await requisitar(contrato, req, params_extra=extra)
            corpo = corpo_ou_erro(r, url=req.url)
            try:
                dados = json.loads(corpo.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ErroAplicacao(
                    codigo="resposta_nao_json",
                    mensagem="The OData service responded with something that is not JSON.",
                    status=502,
                ) from None
            lote = dados.get(campo) if isinstance(dados, dict) else dados
            if not isinstance(lote, list):
                raise ErroAplicacao(
                    codigo="resposta_odata_invalida",
                    mensagem=f"OData response without the '{campo}' list.",
                    status=502,
                )
            paginas += 1
            itens.extend(lote)
            if len(lote) < top:  # short page = done
                break
        else:
            # Hit the page cap. Truncating is legitimate (it protects against a service that ignores
            # $skip and paginates forever), but it MUST be visible: silent truncation becomes a
            # "complete" dataset that is not.
            truncado = True

        payload = json.dumps(
            {campo: itens, "_paginas": paginas, "_truncado": truncado},
            ensure_ascii=False,
        ).encode("utf-8")
        return Payload(
            dados=payload,
            origem_url=req.url,
            tipo_mime="application/json",
            detalhe={"paginas": paginas, "truncado": truncado, "itens": len(itens)},
        )
