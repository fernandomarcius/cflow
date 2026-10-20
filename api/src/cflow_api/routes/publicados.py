"""Reading a published dataset BY RUN: `GET /publicados/{camada}/{dataset}/...`.

`_atual.json` says what is valid now; closing and reporting need what ONE run published, and the
next run of the same scope moves the pointer. The resolution (executed version -> `saida` node ->
immutable run index) lives in `cflow_core.ingestao.publicados`; here live the edge and the
authorisation.

Authorisation at the edge: specialist/admin, the Studio profiles - this is run data, which the
operator consumes through the published page and not through this route. A `sistema` pipeline stays
invisible to anyone who is not admin (generic 403), checked BEFORE any 404 that describes its graph.

`arquivo` delivers the parquet of ONE partition byte for byte, streaming - it is how clients download
bulk datasets (paging `/dados` re-sorts the whole set on every page).

The SERVICE account also reads `dados`, `arquivo` and `indice`. It needs `ver` on the pipeline
(`executar` includes it). Without the permission, and for a nonexistent run, the SAME generic 403 -
a 404 would tell the integrator that the id exists. This is how integrations read gold with a token,
without a personal password. Stamp and scope stay admin-only.

The OBSOLETE stamp on the scope pointer (`/escopo`) is admin-only: it is a write to the object every
consumer of the dataset reads, and the query that precedes it shows the same thing the write
decides. The rule lives in `cflow_core.ingestao.carimbo`.
"""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from cflow_core.authz import permissoes, sistema
from cflow_core.authz.perfis import Perfil
from cflow_core.db import transacao
from cflow_core.errors import DetalheErro, ErroAplicacao
from cflow_core.ingestao import carimbo, publicados
from cflow_core.request_context import request_id_atual

from ..deps import Ator, exigir_ator, exigir_perfil

router = APIRouter(prefix="/publicados")
SoAdmin = Depends(exigir_perfil(Perfil.ADMIN))
Leitor = Depends(exigir_ator(Perfil.ESPECIALISTA, Perfil.ADMIN, Perfil.SERVICO))

# Query params with a role of their own. ANY OTHER name is a partition key (e.g. `project_id`) - and
# `selecionar_particoes` refuses whatever is neither a partition nor a run parameter, so a typo does
# not pass as "no filter".
_PARAMETROS_DA_ROTA = frozenset(
    {"execucao_id", "operacao_id", "competencia", "skip", "take", "filtro", "ordenar", "agregar"}
)


def _negado() -> ErroAplicacao:
    return ErroAplicacao(codigo="acesso_negado", mensagem="Access denied.", status=403)


async def _publicacao(
    camada: str, dataset: str, execucao_id: uuid.UUID, ator: Ator
) -> publicados.Publicacao:
    publicados.exigir_camada(camada)
    servico = ator.perfil == Perfil.SERVICO
    async with transacao() as conn:
        try:
            execucao = await publicados.carregar_execucao(conn, execucao_id)
        except ErroAplicacao as e:
            # For the integrator, "does not exist" and "cannot see" are the same answer.
            if servico and e.status == 404:
                raise _negado() from None
            raise
        await sistema.exigir_acesso(conn, ator.id, "pipeline", execucao["pipeline_id"])
        if servico:
            await permissoes.exigir(conn, ator.id, "pipeline", execucao["pipeline_id"], "ver")
        cfg = await publicados.saida_publicada(conn, execucao, camada, dataset)
    # Outside the transaction: reading the lake (and downloading from the bucket) while holding a
    # pool connection would pin the metadata `xmin` for the duration of the network call.
    return await publicados.abrir_publicacao(execucao, cfg, camada, dataset)


def _filtros_de_particao(
    request: Request, operacao_id: str | None, competencia: str | None
) -> dict[str, str]:
    """`operacao_id`/`competencia` + any extra querystring key. An empty value means "no filter"
    (the web UI sends `""` for a blank field)."""
    filtros: dict[str, str] = {}
    for chave, valor in (("operacao_id", operacao_id), ("competencia", competencia)):
        if valor not in (None, ""):
            filtros[chave] = valor
    for chave in dict.fromkeys(request.query_params.keys()):
        if chave in _PARAMETROS_DA_ROTA:
            continue
        valores = [v for v in request.query_params.getlist(chave) if v != ""]
        if len(valores) > 1:
            raise ErroAplicacao(
                codigo="filtro_particao_invalido",
                mensagem=f"Partition '{chave}' was given more than once; give a single value.",
                status=400,
            )
        if valores:
            filtros[chave] = valores[0]
    return filtros


@router.get("/{camada}/{dataset:path}/dados")
async def ler_dados(
    camada: str,
    dataset: str,
    request: Request,
    execucao_id: uuid.UUID,
    operacao_id: str | None = None,
    competencia: str | None = None,
    skip: int = 0,
    take: int = publicados.TAKE_PADRAO,
    filtro: list[str] = Query(default=[]),
    ordenar: list[str] = Query(default=[]),
    agregar: list[str] = Query(default=[]),
    ator: Ator = Leitor,
) -> dict:
    """Page of the dataset that `execucao_id` published. DECIMAL values and aggregates come out as
    text (the core already converts - the response encoder would turn a `Decimal` into a float).

    Contract of the read parameters:

    - `filtro=column=value` (equals) and `filtro=column~text` (contains, case-insensitive),
      repeatable and combined with AND - except a repeated `=` on the SAME column, which means
      "equals any" (`filtro=ProjectId=A&filtro=ProjectId=B` brings both projects). On a
      DATE/TIME/TIMESTAMP column (with or without timezone, and the nanosecond one) the `=`
      compares in the column type: a value copied from the response itself (`2026-08-06T12:17:23`)
      matches, and a value that is not a date/instant is a 400 `filtro_invalido`. TIMESTAMPTZ comes
      out in the display timezone with the instant offset (`2026-08-06T12:17:23-03:00`); a filter
      without an offset on it is read in that timezone.
    - `ordenar=column` / `ordenar=-column`, repeatable. The page is STABLE: after the requested
      columns (or alone, with none), the order ends in (file, row in file), so scanning all the pages
      returns each row exactly once, even when ordering by a tied key.
    - `agregar=sum:Col1,Col2` and `agregar=count_distinct:Col3`, repeatable and combinable, over the
      whole FILTERED set (not the page). Output per function and under the REQUESTED name (even if
      the column matches ignoring case), with no collision when the same column appears in both:
      `{"sum": {"Col1": "<decimal text>"}, "count_distinct": {"Col3": "<integer text>"}}`. Sum is in
      DECIMAL; `count_distinct` does not count NULL. Empty set: sum `null`, count `"0"`. Without
      `agregar`, `agregados` is `{}`. A value that does not convert is a 400 `agregacao_invalida`
      naming the function and the column; out of memory is a 503 `consulta_sem_memoria`.
    - A run with no partition at all: the schema comes from the index `colunas` (`tipo` comes out
      `null`); an old index without that list is a 409 `esquema_indisponivel`.
    - Any other querystring key is a partition (or run parameter), checked against the run: another
      parameter value is a 404 `publicado_ausente` (a parameter that does not partition is checked by
      type - `as_of` by the instant), an unknown key is a 400."""
    filtros_particao = _filtros_de_particao(request, operacao_id, competencia)
    # Parse pagination/filter before resolving: an invalid parameter is a 400 without touching the lake.
    publicados.validar_paginacao(skip, take)
    pub = await _publicacao(camada, dataset, execucao_id, ator)
    return await publicados.ler_dados(
        pub,
        filtros_particao=filtros_particao,
        skip=skip,
        take=take,
        filtro=filtro,
        ordenar=ordenar,
        agregar=agregar,
    )


@router.get("/{camada}/{dataset:path}/arquivo")
async def baixar_arquivo(
    camada: str,
    dataset: str,
    request: Request,
    execucao_id: uuid.UUID,
    operacao_id: str | None = None,
    competencia: str | None = None,
    ator: Ator = Leitor,
) -> StreamingResponse:
    """The PARQUET that `execucao_id` published for ONE partition, byte for byte, streaming.

    It exists for whoever needs the whole slice (bulk downloads): paging `/dados` re-reads and SORTS
    the set on every page - 190 thousand rows meant 190 pages and about a minute and a half on a
    production-like host. Here the published file goes out as it is, without rewriting, in 1 MiB
    blocks, and the schema arrives in native types (DOUBLE, BIGINT, DECIMAL, TIMESTAMP) instead of
    JSON.

    Contract:

    - Resolution, authorisation and partition selection are those of `/dados` (same `_publicacao`,
      same `operacao_id`/`competencia`/`<key>=value` filters, same 400/403/404). The service account
      needs `ver` on the pipeline; without it, or with a nonexistent run, the same generic 403.
    - The selection must yield ONE file. More than one is a 409 `publicado_varios_arquivos` (the
      message says how many and which partition keys to filter by); none is a 404
      `publicado_ausente`.
    - Headers: `Content-Type: application/vnd.apache.parquet`, `Content-Length` (file bytes),
      `X-Cflow-Linhas` (rows the publication recorded for the partition - missing only in an old
      index without the count) and `X-Cflow-Sha256` (hex sha256 of the bytes sent). The manifest
      stores no parquet digest, so the sha256 is COMPUTED by the server over the same descriptor
      that is sent, before the first byte. The client checks all three: size, sha256 and the parquet
      metadata `num_rows` against `X-Cflow-Linhas`.
    - A file on disk with a size different from the one recorded in the publication: 502
      `publicado_divergente`, nothing sent.
    - `skip`, `take`, `filtro`, `ordenar` and `agregar` belong to `/dados` and are REFUSED here (400
      `parametro_nao_e_do_arquivo`, before touching the lake): the file goes out whole, and
      silently accepting them would deliver all the rows dressed as a slice.
    - Read: leaves no audit entry (like `/dados`)."""
    _recusar_parametros_de_dados(request)
    filtros_particao = _filtros_de_particao(request, operacao_id, competencia)
    pub = await _publicacao(camada, dataset, execucao_id, ator)
    arq = await publicados.abrir_arquivo(pub, filtros_particao)
    cabecalhos = {
        "Content-Length": str(arq.tamanho),
        "X-Cflow-Sha256": arq.sha256,
        "Content-Disposition": f'attachment; filename="{_nome_do_arquivo(dataset, execucao_id)}"',
    }
    if arq.linhas is not None:
        cabecalhos["X-Cflow-Linhas"] = str(arq.linhas)
    return StreamingResponse(
        publicados.em_blocos(arq),
        media_type=publicados.TIPO_PARQUET,
        headers=cabecalhos,
        # If the response never gets to iterate the body, the descriptor is still closed.
        background=BackgroundTask(arq.fechar),
    )


# Parameters that only `/dados` interprets. On `/arquivo` they have no effect - and
# `_filtros_de_particao` skips them because they are in `_PARAMETROS_DA_ROTA` -, so without this
# refusal `filtro=ProjectId=X` would return every project with no error at all. An accepted field
# with no effect is worse than a refused one.
_PARAMETROS_SO_DE_DADOS = ("skip", "take", "filtro", "ordenar", "agregar")


def _recusar_parametros_de_dados(request: Request) -> None:
    presentes = [p for p in _PARAMETROS_SO_DE_DADOS if p in request.query_params]
    if presentes:
        raise ErroAplicacao(
            codigo="parametro_nao_e_do_arquivo",
            mensagem=(
                f"/dados parameter on the file download: {', '.join(presentes)}. The file "
                "goes out whole; to filter, sort, page or aggregate, use /dados."
            ),
            status=400,
            detalhes=[
                DetalheErro(campo=p, mensagem="does not apply to /arquivo; use /dados")
                for p in presentes
            ],
        )


def _nome_do_arquivo(dataset: str, execucao_id: uuid.UUID) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", dataset).strip("_") or "dados"
    return f"{base}-{execucao_id}.parquet"


@router.get("/{camada}/{dataset:path}/indice")
async def ler_indice(
    camada: str, dataset: str, execucao_id: uuid.UUID, ator: Ator = Leitor
) -> dict:
    """The run IMMUTABLE `_indice.json` (with partition) or the `_manifesto.json` (without), as it is
    in the lake - never `_atual.json`, which the next run has already merged."""
    pub = await _publicacao(camada, dataset, execucao_id, ator)
    return pub.documento


# --- OBSOLETE stamp on the scope pointer -------------------------------------------------------


class EscopoIn(BaseModel):
    """Stamp body. LOOSE types on purpose: the format refusal comes from the service, in the standard
    error envelope, instead of FastAPI's generic 422."""

    particao: list = []
    motivo: str | None = None
    simular: bool = False


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/{camada}/{dataset:path}/escopo")
async def consultar_escopo(
    camada: str,
    dataset: str,
    particao: list[str] = Query(default=[]),
    ambiente: str | None = Query(default=None),
    ator: uuid.UUID = SoAdmin,
) -> dict:
    """The pointer of ONE scope and what stamping/unstamping would do to it. `particao=key=value`,
    repeatable, in path order (`?particao=operacao_id=11`). Writes nothing."""
    return await carimbo.consultar(
        camada=camada,
        dataset=dataset,
        particao_bruta=particao,
        ambiente=ambiente,
    )


@router.get("/{camada}/{dataset:path}/escopos-anteriores")
async def descobrir_escopos(
    camada: str,
    dataset: str,
    ambiente: str | None = Query(default=None),
    ator: uuid.UUID = SoAdmin,
) -> dict:
    """The scopes of the PREVIOUS layout, with the state of each pointer and what the stamp would do
    to it. Also works after the first run of the new layout, when the dataset index no longer lists
    them: it comes from the runs of the previous versions and the immutable index of each run. Writes
    nothing."""
    return await carimbo.descobrir(camada=camada, dataset=dataset, ambiente=ambiente)


@router.post("/{camada}/{dataset:path}/escopo/carimbar")
async def carimbar_escopo(
    camada: str,
    dataset: str,
    dados: EscopoIn,
    request: Request,
    ambiente: str | None = Query(default=None),
    ator: uuid.UUID = SoAdmin,
) -> dict:
    """Stamp the pointer of a PREVIOUS-layout scope as OBSOLETE - reads through `publicado` start
    refusing it with `lake_escopo_obsoleto`. Audited in the same transaction; `simular` neither
    writes nor audits; stamping an already stamped scope returns `mudou: false`."""
    return await carimbo.carimbar(
        camada=camada,
        dataset=dataset,
        particao_bruta=dados.particao,
        ambiente=ambiente,
        motivo=dados.motivo,
        ator_id=ator,
        ip=_ip(request),
        request_id=request_id_atual(),
        simular=dados.simular,
    )


@router.post("/{camada}/{dataset:path}/escopo/descarimbar")
async def descarimbar_escopo(
    camada: str,
    dataset: str,
    dados: EscopoIn,
    request: Request,
    ambiente: str | None = Query(default=None),
    ator: uuid.UUID = SoAdmin,
) -> dict:
    """Remove the OBSOLETE mark: the pointer is read again as it was. Audited; requires a reason."""
    return await carimbo.descarimbar(
        camada=camada,
        dataset=dataset,
        particao_bruta=dados.particao,
        ambiente=ambiente,
        motivo=dados.motivo,
        ator_id=ator,
        ip=_ip(request),
        request_id=request_id_atual(),
        simular=dados.simular,
    )
