"""Source file upload (CSV/Excel/Parquet). The file is written to the shared volume
(`upload_dir`, mounted in the api and the worker) and the `caminho` consumed by the 'arquivo'
connector is returned.

Security: the client's file name NEVER becomes part of the stored path (path traversal). An opaque
`{uuid}{ext}` name is generated. File content never reaches the log or the audit diff.
Edge authorization: only specialist/admin (who build the sources) can upload files.
"""

from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, File, Request, UploadFile

from cflow_core.audit import Acao, registrar
from cflow_core.authz.perfis import Perfil
from cflow_core.config import get_settings
from cflow_core.db import transacao
from cflow_core.errors import ErroAplicacao
from cflow_core.request_context import request_id_atual

from ..deps import exigir_perfil

router = APIRouter()
EspecialistaOuAdmin = Depends(exigir_perfil(Perfil.ESPECIALISTA, Perfil.ADMIN))

EXTENSOES_PERMITIDAS = {".csv", ".xlsx", ".xls", ".parquet"}


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/arquivos")
async def enviar_arquivo(
    request: Request, file: UploadFile = File(...), ator: uuid.UUID = EspecialistaOuAdmin
) -> dict:
    nome_original = file.filename or ""
    ext = os.path.splitext(nome_original)[1].lower()
    if ext not in EXTENSOES_PERMITIDAS:
        raise ErroAplicacao(
            codigo="formato_nao_suportado",
            mensagem="Unsupported file format. Accepted: CSV, Excel (xlsx/xls) and Parquet.",
            status=400,
        )

    diretorio = get_settings().upload_dir
    os.makedirs(diretorio, exist_ok=True)
    # opaque name: the client's name never enters the stored path (anti path traversal)
    nome_armazenado = f"{uuid.uuid4().hex}{ext}"
    caminho = os.path.abspath(os.path.join(diretorio, nome_armazenado))

    conteudo = await file.read()
    tamanho = len(conteudo)
    with open(caminho, "wb") as fp:
        fp.write(conteudo)

    async with transacao() as conn:
        await registrar(
            conn,
            acao=Acao.CRIAR,
            recurso_tipo="arquivo",
            recurso_id=uuid.uuid4(),
            ator_id=ator,
            antes=None,
            depois={
                "nome_original": nome_original,
                "caminho": caminho,
                "tamanho": tamanho,
            },  # NO content
            ip=_ip(request),
            request_id=request_id_atual(),
        )

    return {"caminho": caminho, "nome_original": nome_original, "tamanho": tamanho}
