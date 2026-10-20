"""FastAPI application. Registers the request_id middleware and the error handlers that return
the standard error envelope. This is the ONLY entry point.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from cflow_core.config import get_settings
from cflow_core.errors import CorpoErro, EnvelopeErro, ErroAplicacao
from cflow_core.logging import configurar_logging
from cflow_core.request_context import CABECALHO_REQUEST_ID, definir_request_id

from .routes import (
    admin,
    arquivos,
    auditoria,
    auth,
    conexoes,
    cubos,
    funcoes,
    grupos,
    health,
    ingestao,
    paginas,
    permissoes,
    pipelines,
    publicados,
    relatorios,
    usuarios,
    views,
)

logger = logging.getLogger("cflow_api")

#: Public prefix under which a reverse proxy may expose the API. Requests that arrive with it are
#: served by the same routes (`/api/v1/pipelines` == `/pipelines`), so the API works both behind a
#: proxy that strips the prefix and when addressed directly with it.
PREFIXO_PUBLICO = "/api/v1"


def criar_app() -> FastAPI:
    settings = get_settings()
    configurar_logging(settings.log_level)

    app = FastAPI(title="cFlow API", version="0.1.0")

    @app.middleware("http")
    async def prefixo_publico(request: Request, call_next):
        caminho = request.scope["path"]
        if caminho == PREFIXO_PUBLICO or caminho.startswith(PREFIXO_PUBLICO + "/"):
            request.scope["path"] = caminho[len(PREFIXO_PUBLICO) :] or "/"
        return await call_next(request)

    @app.middleware("http")
    async def contexto_request_id(request: Request, call_next):
        request_id = definir_request_id(request.headers.get(CABECALHO_REQUEST_ID))
        logger.info(
            "requisicao",
            extra={"extra_campos": {"metodo": request.method, "rota": request.url.path}},
        )
        try:
            resposta = await call_next(request)
        except Exception:
            logger.exception("erro_nao_tratado")
            corpo = EnvelopeErro(
                erro=CorpoErro(
                    codigo="erro_interno",
                    mensagem="An internal error occurred. Please try again.",
                    request_id=request_id,
                )
            )
            resposta = JSONResponse(status_code=500, content=corpo.model_dump())
        resposta.headers[CABECALHO_REQUEST_ID] = request_id
        return resposta

    @app.exception_handler(ErroAplicacao)
    async def tratar_erro_aplicacao(request: Request, exc: ErroAplicacao):
        from cflow_core.request_context import request_id_atual

        request_id = request_id_atual() or ""
        logger.warning("erro_aplicacao", extra={"extra_campos": {"codigo": exc.codigo}})
        resposta = JSONResponse(
            status_code=exc.status, content=exc.envelope(request_id).model_dump()
        )
        resposta.headers[CABECALHO_REQUEST_ID] = request_id
        return resposta

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(usuarios.router)
    app.include_router(grupos.router)
    app.include_router(permissoes.router)
    app.include_router(auditoria.router)
    app.include_router(conexoes.router)
    app.include_router(arquivos.router)
    app.include_router(pipelines.router)
    app.include_router(admin.router)
    app.include_router(cubos.router)
    app.include_router(views.router)
    # BEFORE `paginas`: there `GET /relatorios/{relatorio_id}` takes a uuid parameter and the
    # router matches in order - registered later, `/relatorios/exportacoes` would fall into the
    # uuid route and return 422 instead of reaching here.
    app.include_router(relatorios.router)
    app.include_router(paginas.router)
    app.include_router(funcoes.router)
    app.include_router(ingestao.router)
    # Own `/publicados` prefix: no router above matches that path, so the position competes with
    # no one (unlike `relatorios` x `paginas`).
    app.include_router(publicados.router)
    return app


app = criar_app()
