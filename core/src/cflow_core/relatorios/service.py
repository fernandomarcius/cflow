"""Exports: a report is the ARTIFACT of a run, addressed by the tuple
`(execucao_id, no_id, layout_id, layout_versao, formato)` - never a recalculated request.

- **Identity = `execucao_id` + `no_id`**. A higher-level caller that resolves the tuple itself can
  call `solicitar` with it; nothing here changes.
- **Only snapshots of COMPLETED runs**: 409 `execucao_nao_concluida` before any INSERT. A running
  or failed run has no report.
- **Idempotent by key**: two identical requests converge to ONE record and ONE file. Uniqueness is
  enforced by the database (partial index `estado <> 'falha'`), not by a SELECT before the INSERT -
  two workers on the same click would both pass. A failure stays as evidence and the next attempt
  is a new record.
- **`Acao.EXPORTAR` in the same transaction** as the INSERT; null `gerado_por` = eager generation
  by the platform (`ator_tipo='sistema'`).
- **Eager generation**: when a run completes, every DEFAULT layout published for one of its nodes
  is enqueued, smallest snapshot first. A click that arrives earlier falls into the `gerando` flow.
- **Grouped**: ONE file with the sheets of several single-source layouts. Identity is the set of
  parts `(aba, execucao_id, no_id, layout_id, layout_versao)` + grouped layout + version + format -
  same key, same index, same states. In eager mode it is created when the LAST run among the parts
  of the SAME chain completes, in any order.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..audit import Acao, AtorTipo, registrar
from ..config import get_settings
from ..db import transacao
from ..errors import ErroAplicacao
from ..execucao import snapshots
from ..ingestao import armazenamento
from ..modelagem import versionado
from ..timezone import para_exibicao
from . import gerar as _gerar
from . import layout as _layout
from . import processo_xlsx as _processo

# The module's original `gerar_xlsx`. If someone replaces it in memory (test, extension), the
# replacement only exists in THIS process - so generation stays in the thread (see `_gerar_e_gravar`).
_GERAR_XLSX_ORIGINAL = _gerar.gerar_xlsx
_GERAR_AGRUPADO_ORIGINAL = _gerar.gerar_xlsx_agrupado

logger = logging.getLogger("cflow_core.relatorios")

CAMADA = "relatorios"
FORMATOS = ("xlsx",)
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
RECURSO_LAYOUT = "relatorio_layout"
RECURSO_EXPORTACAO = "relatorio_exportacao"
_T = {
    "tabela": "relatorio_layouts",
    "versoes": "relatorio_layout_versoes",
    "idcol": "layout_id",
    "recurso_tipo": RECURSO_LAYOUT,
}
# Interval between `linhas_escritas` writes while the file is being generated.
_PROGRESSO_SEGUNDOS = 2.0

Despachador = Callable[[uuid.UUID], Awaitable[None]]


# --- Layouts ----------------------------------------------------------------------------------


async def criar_layout(
    conn: AsyncConnection,
    *,
    nome: str,
    descricao: str | None,
    dono_id: uuid.UUID,
    ip=None,
    request_id=None,
) -> uuid.UUID:
    return await versionado.criar(
        conn,
        tabela=_T["tabela"],
        recurso_tipo=RECURSO_LAYOUT,
        nome=nome,
        descricao=descricao,
        dono_id=dono_id,
        ip=ip,
        request_id=request_id,
    )


async def _conferir_fonte(conn: AsyncConnection, definicao: dict) -> dict | None:
    """The source pipeline must exist and the node must be in its latest definition. Without this,
    the layout would publish pointing at a node no run will ever have - and the error would only
    show up on export click, far from whoever wrote the layout."""
    from ..pipelines import service as pipelines

    pid = uuid.UUID(str(definicao["fonte"]["pipeline"]))
    try:
        det = await pipelines.obter(conn, pid)
    except ErroAplicacao as exc:
        if exc.codigo == "nao_encontrado":
            raise ErroAplicacao(
                codigo="layout_pipeline_inexistente",
                mensagem="The pipeline referenced in 'fonte.pipeline' does not exist.",
                status=400,
            ) from None
        raise
    nos = {n.get("id") for n in det["definicao"].get("nos", [])}
    if nos and definicao["fonte"]["no"] not in nos:
        raise ErroAplicacao(
            codigo="layout_no_inexistente",
            mensagem=f"Node '{definicao['fonte']['no']}' does not exist in pipeline '{det['nome']}'.",
            status=400,
        )
    return det


async def _layouts_publicados(conn: AsyncConnection, ids: list[str]) -> dict[str, dict]:
    """`{layout_id: {id, nome, numero, definicao}}` for the requested layouts, with the PUBLISHED
    version (`numero`/`definicao` null when there is none)."""
    if not ids:
        return {}
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT l.id, l.nome, v.numero, v.definicao FROM relatorio_layouts l "
                    "LEFT JOIN relatorio_layout_versoes v ON v.id = l.versao_publicada_id "
                    "WHERE l.id = ANY(:ids)"
                ),
                {"ids": [uuid.UUID(i) for i in ids]},
            )
        )
        .mappings()
        .all()
    )
    return {str(r["id"]): dict(r) for r in rows}


async def _conferir_partes_do_agrupado(conn: AsyncConnection, definicao: dict) -> None:
    """Every sheet points at a layout that EXISTS, is PUBLISHED and is single-source. Without this,
    the grouped layout would publish pointing at something no export can assemble - and the error
    would only show up on the export click, far from whoever wrote the layout."""
    abas = _layout.abas_do_agrupado(definicao)
    achados = await _layouts_publicados(conn, [lid for _, lid in abas])
    for aba, lid in abas:
        lay = achados.get(lid)
        if lay is None:
            raise ErroAplicacao(
                codigo="layout_parte_inexistente",
                mensagem=f"Sheet '{aba}' points at layout {lid}, which does not exist.",
                status=400,
            )
        if lay["numero"] is None:
            raise ErroAplicacao(
                codigo="layout_parte_nao_publicada",
                mensagem=f"Sheet '{aba}' points at layout '{lay['nome']}', which has no published "
                "version.",
                status=400,
            )
        if _layout.eh_agrupado(lay["definicao"]):
            raise ErroAplicacao(
                codigo="layout_agrupado_aninhado",
                mensagem=f"Sheet '{aba}' points at grouped layout '{lay['nome']}'; each sheet "
                "lays out the snapshot of ONE node.",
                status=400,
            )


async def conferir_contra_ultimo_snapshot(conn: AsyncConnection, definicao: dict) -> dict:
    """Check the layout's columns against the node's LATEST completed snapshot, if any. With no
    completed run there is nothing to check - publish with a warning; the definitive check happens
    on the export request, against the exact snapshot."""
    pid = uuid.UUID(str(definicao["fonte"]["pipeline"]))
    try:
        snap = await snapshots.resolver_snapshot(conn, pid, definicao["fonte"]["no"])
    except ErroAplicacao as exc:
        if exc.codigo in ("sem_execucao", "snapshot_ausente"):
            return {
                "conferido": False,
                "aviso": "The node has no completed run with a snapshot yet; columns will be "
                "checked on export.",
            }
        raise
    _layout.validar_contra_snapshot(definicao, _gerar.colunas_do_parquet(snap["snapshot"]))
    return {"conferido": True, "execucao_id": snap["execucao_id"]}


async def salvar_rascunho_layout(
    conn: AsyncConnection,
    *,
    layout_id: uuid.UUID,
    definicao: dict,
    base_numero: int,
    autor_id: uuid.UUID,
    ip=None,
    request_id=None,
) -> int:
    _layout.validar(definicao)
    if _layout.eh_agrupado(definicao):
        await _conferir_partes_do_agrupado(conn, definicao)
    else:
        await _conferir_fonte(conn, definicao)
    return await versionado.salvar_rascunho(
        conn,
        versoes=_T["versoes"],
        idcol=_T["idcol"],
        recurso_tipo=RECURSO_LAYOUT,
        recurso_id=layout_id,
        definicao=definicao,
        base_numero=base_numero,
        autor_id=autor_id,
        validar=_layout.validar,
        ip=ip,
        request_id=request_id,
    )


async def publicar_layout(
    conn: AsyncConnection,
    *,
    layout_id: uuid.UUID,
    base_numero: int,
    nota: str | None,
    autor_id: uuid.UUID,
    nome: str | None = None,
    ip=None,
    request_id=None,
) -> dict:
    """Publish the latest version. Besides structural validation, check the source (pipeline + node)
    and the columns against the latest completed snapshot - a nonexistent column is an error at
    publish time, not an empty column in the file."""
    atual = await versionado.obter(
        conn,
        tabela=_T["tabela"],
        versoes=_T["versoes"],
        idcol=_T["idcol"],
        recurso_id=layout_id,
    )
    definicao = atual["definicao"]
    if not definicao:
        raise ErroAplicacao(
            codigo="sem_definicao", mensagem="There is no definition to publish.", status=400
        )
    _layout.validar(definicao)
    if _layout.eh_agrupado(definicao):
        await _conferir_partes_do_agrupado(conn, definicao)
        conferencia = {
            "conferido": False,
            "aviso": "Each sheet's columns come from the single-source layout it points at; they are "
            "checked against each part's exact snapshot on export.",
        }
    else:
        await _conferir_fonte(conn, definicao)
        conferencia = await conferir_contra_ultimo_snapshot(conn, definicao)
    numero = await versionado.publicar(
        conn,
        tabela=_T["tabela"],
        versoes=_T["versoes"],
        idcol=_T["idcol"],
        recurso_tipo=RECURSO_LAYOUT,
        recurso_id=layout_id,
        base_numero=base_numero,
        nota=nota,
        autor_id=autor_id,
        validar=_layout.validar,
        nome=nome,
        ip=ip,
        request_id=request_id,
    )
    return {"numero": numero, **conferencia}


async def obter_layout(
    conn: AsyncConnection, layout_id: uuid.UUID, numero: int | None = None
) -> dict:
    det = await versionado.obter(
        conn,
        tabela=_T["tabela"],
        versoes=_T["versoes"],
        idcol=_T["idcol"],
        recurso_id=layout_id,
        numero=numero,
    )
    pub = (
        await conn.execute(
            text(
                "SELECT v.numero FROM relatorio_layouts l "
                "JOIN relatorio_layout_versoes v ON v.id = l.versao_publicada_id WHERE l.id = :r"
            ),
            {"r": layout_id},
        )
    ).scalar_one_or_none()
    return {"id": str(layout_id), **det, "versao_publicada": pub}


async def listar_layouts(
    conn: AsyncConnection, *, pipeline_id: uuid.UUID | None = None, tipo: str | None = None
) -> list:
    """Layouts with their published version. `tipo` (`simples` | `agrupado`) filters; a grouped
    layout has no pipeline or node (`null`) and carries `abas`: `[{aba, layout_id}]` in file order."""
    sql = (
        "SELECT l.id, l.nome, l.descricao, l.criado_em, v.numero AS versao_publicada, "
        "v.definicao->'fonte'->>'pipeline' AS pipeline_id, v.definicao->'fonte'->>'no' AS no_id, "
        "coalesce((v.definicao->>'padrao')::boolean, false) AS padrao, "
        "coalesce(v.definicao->>'tipo', 'simples') AS tipo, v.definicao->'abas' AS abas "
        "FROM relatorio_layouts l "
        "LEFT JOIN relatorio_layout_versoes v ON v.id = l.versao_publicada_id"
    )
    filtros: list[str] = []
    params: dict = {}
    if pipeline_id is not None:
        filtros.append("v.definicao->'fonte'->>'pipeline' = :p")
        params["p"] = str(pipeline_id)
    if tipo is not None:
        if tipo not in (_layout.TIPO_SIMPLES, _layout.TIPO_AGRUPADO):
            raise ErroAplicacao(
                codigo="parametro_invalido",
                mensagem=f"'tipo' accepts '{_layout.TIPO_SIMPLES}' or '{_layout.TIPO_AGRUPADO}'.",
                status=400,
            )
        # A layout with no published version has no type to assert: it stays out of the filter.
        filtros.append("v.id IS NOT NULL AND coalesce(v.definicao->>'tipo', 'simples') = :t")
        params["t"] = tipo
    if filtros:
        sql += " WHERE " + " AND ".join(filtros)
    rows = (await conn.execute(text(sql + " ORDER BY l.nome"), params)).mappings().all()
    return [
        {
            "id": str(r["id"]),
            "nome": r["nome"],
            "descricao": r["descricao"],
            "versao_publicada": r["versao_publicada"],
            "tipo": r["tipo"] if r["versao_publicada"] is not None else None,
            "pipeline_id": r["pipeline_id"],
            "no_id": r["no_id"],
            "abas": (
                [{"aba": a.get("aba"), "layout_id": a.get("layout")} for a in r["abas"]]
                if isinstance(r["abas"], list)
                else None
            ),
            "padrao": bool(r["padrao"]),
            "criado_em": para_exibicao(r["criado_em"]).isoformat(),
        }
        for r in rows
    ]


async def excluir_layout(
    conn: AsyncConnection, *, layout_id: uuid.UUID, ator_id: uuid.UUID, ip=None, request_id=None
) -> None:
    await versionado.excluir(
        conn,
        tabela=_T["tabela"],
        versoes=_T["versoes"],
        idcol=_T["idcol"],
        recurso_tipo=RECURSO_LAYOUT,
        recurso_id=layout_id,
        ator_id=ator_id,
        guardas=[
            (
                "SELECT count(*) FROM relatorio_exportacoes WHERE layout_id = :r",
                "layout_com_exportacao",
                "This layout has already generated an export - the artifact is immutable and points "
                "to it. Publish a new version instead of deleting.",
            ),
            (
                "SELECT count(*) FROM relatorio_exportacao_partes WHERE layout_id = :r",
                "layout_com_exportacao",
                "This layout is already a sheet of a grouped export - the artifact is immutable and "
                "points to it. Publish a new version instead of deleting.",
            ),
            (
                "SELECT count(*) FROM relatorio_layouts l "
                "JOIN relatorio_layout_versoes v ON v.id = l.versao_publicada_id "
                "WHERE v.definicao->>'tipo' = 'agrupado' AND v.definicao->'abas' @> "
                "jsonb_build_array(jsonb_build_object('layout', CAST(CAST(:r AS uuid) AS text)))",
                "layout_em_agrupado",
                "This layout is a sheet of a published grouped layout; deleting it would leave the "
                "grouped layout without that sheet. Remove the sheet from the grouped layout (new "
                "version) first.",
            ),
        ],
        ip=ip,
        request_id=request_id,
    )


# --- Exports ----------------------------------------------------------------------------------


def chave_de(
    execucao_id: uuid.UUID | str,
    no_id: str,
    layout_id: uuid.UUID | str,
    layout_versao: int,
    formato: str,
) -> str:
    """Hash of the tuple that identifies the artifact. Deterministic: this is what makes "same
    click, same bytes"."""
    base = json.dumps(
        [str(execucao_id), no_id, str(layout_id), int(layout_versao), formato],
        separators=(",", ":"),
    )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def chave_agrupada(
    layout_id: uuid.UUID | str, layout_versao: int, formato: str, partes: list[dict]
) -> str:
    """Hash of a grouped export: grouped layout + version + format + ALL parts, in sheet order -
    `(aba, execucao_id, no_id, layout_id, layout_versao)`. A part from another run, or a
    republished part layout, is a different artifact. The `agrupado` prefix keeps the key space
    apart from single-source exports."""
    base = json.dumps(
        [
            "agrupado",
            str(layout_id),
            int(layout_versao),
            formato,
            [
                [
                    p["aba"],
                    str(p["execucao_id"]),
                    p["no_id"],
                    str(p["layout_id"]),
                    int(p["layout_versao"]),
                ]
                for p in partes
            ],
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _slug(texto: str) -> str:
    """File name: no accents, no path, no spaces - Content-Disposition and the browser's
    `a.download` must agree."""
    sem_acento = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "_", sem_acento).strip("_")
    return s[:60] or "relatorio"


def caminho_no_lake(chave: str, formato: str) -> str:
    return f"{chave}/arquivo.{formato}"


async def _execucao(conn: AsyncConnection, execucao_id: uuid.UUID) -> dict:
    e = (
        (
            await conn.execute(
                text(
                    "SELECT e.id, e.pipeline_id, e.versao_numero, e.status, e.cadeia_id, "
                    "p.nome AS pipeline_nome "
                    "FROM execucoes e JOIN pipelines p ON p.id = e.pipeline_id WHERE e.id = :id"
                ),
                {"id": execucao_id},
            )
        )
        .mappings()
        .first()
    )
    if e is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Run not found.", status=404)
    return dict(e)


async def _no_concluido(conn: AsyncConnection, execucao_id: uuid.UUID, no_id: str) -> dict:
    n = (
        (
            await conn.execute(
                text(
                    "SELECT no_id, status, linhas, hash, snapshot FROM execucao_nos "
                    "WHERE execucao_id = :e AND no_id = :n"
                ),
                {"e": execucao_id, "n": no_id},
            )
        )
        .mappings()
        .first()
    )
    if n is None:
        raise ErroAplicacao(
            codigo="no_nao_encontrado",
            mensagem=f"Node '{no_id}' is not part of this run.",
            status=404,
        )
    if n["status"] != "concluida" or not n["snapshot"]:
        raise ErroAplicacao(
            codigo="no_nao_concluido",
            mensagem=f"Node '{no_id}' did not complete in this run - there is no snapshot to export.",
            status=409,
        )
    if not os.path.isfile(n["snapshot"]):
        raise ErroAplicacao(
            codigo="snapshot_ausente",
            mensagem=f"The snapshot of node '{no_id}' is no longer on the server (purged?).",
            status=409,
        )
    return dict(n)


async def _layout_publicado(conn: AsyncConnection, layout_id: uuid.UUID) -> dict:
    r = (
        (
            await conn.execute(
                text(
                    "SELECT l.id, l.nome, v.numero, v.definicao FROM relatorio_layouts l "
                    "LEFT JOIN relatorio_layout_versoes v ON v.id = l.versao_publicada_id "
                    "WHERE l.id = :id"
                ),
                {"id": layout_id},
            )
        )
        .mappings()
        .first()
    )
    if r is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Layout not found.", status=404)
    if r["numero"] is None:
        raise ErroAplicacao(
            codigo="layout_nao_publicado",
            mensagem=f"Layout '{r['nome']}' has no published version.",
            status=409,
        )
    return dict(r)


async def solicitar(
    *,
    execucao_id: uuid.UUID,
    no_id: str,
    layout_id: uuid.UUID,
    ator_id: uuid.UUID | None,
    formato: str = "xlsx",
    ip: str | None = None,
    request_id: str | None = None,
    despachar: Despachador | None = None,
) -> dict:
    """Request the artifact. Returns `{"exportacao": {...}, "criada": bool}` - `criada=False` when
    the request converged to an already existing record (ready or still generating).

    All checks (completed run, node with snapshot, layout published and pointing at THIS
    pipeline/node, existing columns) happen BEFORE the INSERT: an error here is immediate and says
    what is missing; an error later would be a `falha` export the user only finds out about while
    polling."""
    if formato not in FORMATOS:
        raise ErroAplicacao(
            codigo="formato_invalido",
            mensagem=f"Format '{formato}' is not supported (use {', '.join(FORMATOS)}).",
            status=400,
        )
    criada = False
    async with transacao() as conn:
        execucao = await _execucao(conn, execucao_id)
        if execucao["status"] != "concluida":
            raise ErroAplicacao(
                codigo="execucao_nao_concluida",
                mensagem="Only snapshots of completed runs can be exported - this one is "
                f"'{execucao['status']}'.",
                status=409,
            )
        no = await _no_concluido(conn, execucao_id, no_id)
        lay = await _layout_publicado(conn, layout_id)
        definicao = lay["definicao"]
        if _layout.eh_agrupado(definicao):
            raise ErroAplicacao(
                codigo="layout_agrupado_exige_partes",
                mensagem=f"Layout '{lay['nome']}' is grouped: request it with 'partes' (sheet -> run), "
                "not with a run and a node.",
                status=400,
            )
        fonte = definicao.get("fonte") or {}
        if str(fonte.get("pipeline")) != str(execucao["pipeline_id"]) or fonte.get("no") != no_id:
            raise ErroAplicacao(
                codigo="layout_nao_corresponde",
                mensagem=f"Layout '{lay['nome']}' belongs to node '{fonte.get('no')}' of pipeline "
                f"{fonte.get('pipeline')}, not to this node/run.",
                status=409,
            )
        _layout.validar_contra_snapshot(definicao, _gerar.colunas_do_parquet(no["snapshot"]))

        chave = chave_de(execucao_id, no_id, layout_id, lay["numero"], formato)
        nome_arquivo = (
            f"{_slug(lay['nome'])}_{_slug(execucao['pipeline_nome'])}_"
            f"{str(execucao_id)[:8]}.{formato}"
        )
        novo_id = uuid.uuid4()
        inserido = (
            await conn.execute(
                text(
                    "INSERT INTO relatorio_exportacoes (id, chave, execucao_id, no_id, layout_id, "
                    "layout_versao, formato, estado, linhas_total, nome_arquivo, gerado_por) "
                    "VALUES (:id, :chave, :e, :n, :l, :lv, :f, 'gerando', :lt, :nome, :g) "
                    "ON CONFLICT (chave) WHERE estado <> 'falha' DO NOTHING RETURNING id"
                ),
                {
                    "id": novo_id,
                    "chave": chave,
                    "e": execucao_id,
                    "n": no_id,
                    "l": layout_id,
                    "lv": lay["numero"],
                    "f": formato,
                    "lt": no["linhas"],
                    "nome": nome_arquivo,
                    "g": ator_id,
                },
            )
        ).scalar_one_or_none()
        if inserido is not None:
            criada = True
            await registrar(
                conn,
                acao=Acao.EXPORTAR,
                recurso_tipo=RECURSO_EXPORTACAO,
                recurso_id=novo_id,
                ator_id=ator_id,
                ator_tipo=AtorTipo.USUARIO if ator_id else AtorTipo.SISTEMA,
                antes=None,
                depois={
                    "execucao_id": str(execucao_id),
                    "no_id": no_id,
                    "layout_id": str(layout_id),
                    "layout_versao": lay["numero"],
                    "formato": formato,
                    "chave": chave,
                    "nome_arquivo": nome_arquivo,
                },
                request_id=request_id,
                ip=ip,
            )
            exportacao_id = novo_id
        else:
            exportacao_id = (
                await conn.execute(
                    text(
                        "SELECT id FROM relatorio_exportacoes WHERE chave = :c AND estado <> 'falha'"
                    ),
                    {"c": chave},
                )
            ).scalar_one()
        detalhe = await obter(conn, exportacao_id)

    if criada:
        await (despachar or despachar_padrao)(exportacao_id)
        async with transacao() as conn:
            detalhe = await obter(conn, exportacao_id)
    return {"exportacao": detalhe, "criada": criada}


def _na_aba(aba: str, exc: ErroAplicacao) -> ErroAplicacao:
    """The same error, saying WHICH sheet: a bare '409 execucao_nao_concluida' forces the caller
    to guess which of the parts is missing."""
    return ErroAplicacao(
        codigo=exc.codigo,
        mensagem=f"Sheet '{aba}': {exc.mensagem}",
        status=exc.status,
        detalhes=exc.detalhes,
    )


async def _resolver_parte(
    conn: AsyncConnection, aba: str, layout_parte_id: str, execucao_id: uuid.UUID
) -> dict:
    """Check ONE sheet of the grouped export, with the same rules as a single-source export:
    completed run, published layout, single-source and of this pipeline, node with snapshot,
    existing columns. Returns the resolved part (the layout version is the one PUBLISHED now)."""
    try:
        execucao = await _execucao(conn, execucao_id)
        if execucao["status"] != "concluida":
            raise ErroAplicacao(
                codigo="execucao_nao_concluida",
                mensagem="Only snapshots of completed runs can be exported - this one is "
                f"'{execucao['status']}'.",
                status=409,
            )
        lay = await _layout_publicado(conn, uuid.UUID(layout_parte_id))
        definicao = lay["definicao"]
        if _layout.eh_agrupado(definicao):
            raise ErroAplicacao(
                codigo="layout_agrupado_aninhado",
                mensagem=f"Layout '{lay['nome']}' is grouped; each sheet lays out ONE node.",
                status=409,
            )
        fonte = definicao.get("fonte") or {}
        if str(fonte.get("pipeline")) != str(execucao["pipeline_id"]):
            raise ErroAplicacao(
                codigo="layout_nao_corresponde",
                mensagem=f"Layout '{lay['nome']}' belongs to pipeline {fonte.get('pipeline')}, not to "
                f"pipeline '{execucao['pipeline_nome']}' of run {execucao_id}.",
                status=409,
            )
        no = await _no_concluido(conn, execucao_id, fonte["no"])
        _layout.validar_contra_snapshot(definicao, _gerar.colunas_do_parquet(no["snapshot"]))
    except ErroAplicacao as exc:
        raise _na_aba(aba, exc) from None
    return {
        "aba": aba,
        "execucao_id": execucao_id,
        "no_id": fonte["no"],
        "layout_id": uuid.UUID(layout_parte_id),
        "layout_versao": lay["numero"],
        "linhas": no["linhas"] or 0,
        "cadeia_id": execucao["cadeia_id"],
    }


async def solicitar_agrupado(
    *,
    layout_id: uuid.UUID,
    partes: dict[str, uuid.UUID | str],
    ator_id: uuid.UUID | None,
    formato: str = "xlsx",
    ip: str | None = None,
    request_id: str | None = None,
    despachar: Despachador | None = None,
) -> dict:
    """Request the GROUPED artifact: `partes` is `{sheet: execucao_id}` - one run per sheet of the
    grouped layout, no more, no less. Same contract as `solicitar`: returns
    `{"exportacao", "criada"}`, everything checked BEFORE the INSERT (per sheet, with its name in
    the error), idempotent by the parts key, audit trail in the same transaction.

    The caller chooses the runs - a facade by chain, or the eager path by the run that just
    completed. `cadeia_id` is only stored when ALL of them come from the same chain."""
    if formato not in FORMATOS:
        raise ErroAplicacao(
            codigo="formato_invalido",
            mensagem=f"Format '{formato}' is not supported (use {', '.join(FORMATOS)}).",
            status=400,
        )
    criada = False
    async with transacao() as conn:
        lay = await _layout_publicado(conn, layout_id)
        definicao = lay["definicao"]
        if not _layout.eh_agrupado(definicao):
            raise ErroAplicacao(
                codigo="layout_nao_e_agrupado",
                mensagem=f"Layout '{lay['nome']}' is single-source: request it with 'execucao_id' and "
                "'no_id', not with 'partes'.",
                status=400,
            )
        abas = _layout.abas_do_agrupado(definicao)
        nomes = [a for a, _ in abas]
        faltam = [a for a in nomes if a not in partes]
        sobram = sorted(a for a in partes if a not in nomes)
        if faltam or sobram:
            raise ErroAplicacao(
                codigo="partes_nao_conferem",
                mensagem=f"Layout '{lay['nome']}' has the sheets {', '.join(nomes)}; "
                + (f"missing the run for: {', '.join(faltam)}. " if faltam else "")
                + (f"sheet it does not have: {', '.join(sobram)}." if sobram else ""),
                status=400,
            )
        resolvidas = []
        for aba, lid in abas:
            try:
                eid = uuid.UUID(str(partes[aba]))
            except ValueError:
                raise ErroAplicacao(
                    codigo="partes_nao_conferem",
                    mensagem=f"Sheet '{aba}': '{partes[aba]}' is not a run id.",
                    status=400,
                ) from None
            resolvidas.append(await _resolver_parte(conn, aba, lid, eid))
        cadeias = {p["cadeia_id"] for p in resolvidas}
        cadeia_id = next(iter(cadeias)) if len(cadeias) == 1 else None

        chave = chave_agrupada(layout_id, lay["numero"], formato, resolvidas)
        nome_arquivo = f"{_slug(lay['nome'])}_{str(cadeia_id or chave)[:8]}.{formato}"
        novo_id = uuid.uuid4()
        inserido = (
            await conn.execute(
                text(
                    "INSERT INTO relatorio_exportacoes (id, chave, tipo, cadeia_id, layout_id, "
                    "layout_versao, formato, estado, linhas_total, nome_arquivo, gerado_por) "
                    "VALUES (:id, :chave, 'agrupado', :c, :l, :lv, :f, 'gerando', :lt, :nome, :g) "
                    "ON CONFLICT (chave) WHERE estado <> 'falha' DO NOTHING RETURNING id"
                ),
                {
                    "id": novo_id,
                    "chave": chave,
                    "c": cadeia_id,
                    "l": layout_id,
                    "lv": lay["numero"],
                    "f": formato,
                    "lt": sum(p["linhas"] for p in resolvidas),
                    "nome": nome_arquivo,
                    "g": ator_id,
                },
            )
        ).scalar_one_or_none()
        if inserido is not None:
            criada = True
            for ordem, p in enumerate(resolvidas, start=1):
                await conn.execute(
                    text(
                        "INSERT INTO relatorio_exportacao_partes (exportacao_id, ordem, aba, "
                        "execucao_id, no_id, layout_id, layout_versao) "
                        "VALUES (:x, :o, :a, :e, :n, :l, :lv)"
                    ),
                    {
                        "x": novo_id,
                        "o": ordem,
                        "a": p["aba"],
                        "e": p["execucao_id"],
                        "n": p["no_id"],
                        "l": p["layout_id"],
                        "lv": p["layout_versao"],
                    },
                )
            await registrar(
                conn,
                acao=Acao.EXPORTAR,
                recurso_tipo=RECURSO_EXPORTACAO,
                recurso_id=novo_id,
                ator_id=ator_id,
                ator_tipo=AtorTipo.USUARIO if ator_id else AtorTipo.SISTEMA,
                antes=None,
                depois={
                    "tipo": "agrupado",
                    "layout_id": str(layout_id),
                    "layout_versao": lay["numero"],
                    "formato": formato,
                    "chave": chave,
                    "nome_arquivo": nome_arquivo,
                    "cadeia_id": str(cadeia_id) if cadeia_id else None,
                    "partes": [
                        {
                            "aba": p["aba"],
                            "execucao_id": str(p["execucao_id"]),
                            "no_id": p["no_id"],
                            "layout_id": str(p["layout_id"]),
                            "layout_versao": p["layout_versao"],
                        }
                        for p in resolvidas
                    ],
                },
                request_id=request_id,
                ip=ip,
            )
            exportacao_id = novo_id
        else:
            exportacao_id = (
                await conn.execute(
                    text(
                        "SELECT id FROM relatorio_exportacoes WHERE chave = :c AND estado <> 'falha'"
                    ),
                    {"c": chave},
                )
            ).scalar_one()
        detalhe = await obter(conn, exportacao_id)

    if criada:
        await (despachar or despachar_padrao)(exportacao_id)
        async with transacao() as conn:
            detalhe = await obter(conn, exportacao_id)
    return {"exportacao": detalhe, "criada": criada}


async def despachar_padrao(exportacao_id: uuid.UUID) -> None:
    """Temporal (dedicated `cflow-relatorios` queue, one file = one activity) or, without Temporal,
    inline generation - same function, same result."""
    cfg = get_settings()
    if cfg.usar_temporal:
        from temporalio.client import Client

        cliente = await Client.connect(cfg.temporal_host)
        await cliente.start_workflow(
            "GerarRelatorio",
            args=[str(exportacao_id)],
            id=f"relatorio-{exportacao_id}",
            task_queue=cfg.relatorios_task_queue,
        )
        return
    await gerar(exportacao_id)


async def _detalhe_para_gerar(conn: AsyncConnection, exportacao_id: uuid.UUID) -> dict:
    r = (
        (
            await conn.execute(
                text(
                    "SELECT x.*, e.pipeline_id, e.versao_numero, e.parametros, "
                    "p.nome AS pipeline_nome, "
                    "       l.nome AS layout_nome, v.definicao, "
                    "       n.snapshot, n.hash AS hash_snapshot, n.linhas AS linhas_snapshot, "
                    "       u.nome AS gerado_por_nome, u.email AS gerado_por_email "
                    "FROM relatorio_exportacoes x "
                    "LEFT JOIN execucoes e ON e.id = x.execucao_id "
                    "LEFT JOIN pipelines p ON p.id = e.pipeline_id "
                    "JOIN relatorio_layouts l ON l.id = x.layout_id "
                    "JOIN relatorio_layout_versoes v ON v.layout_id = x.layout_id "
                    "     AND v.numero = x.layout_versao "
                    "LEFT JOIN execucao_nos n ON n.execucao_id = x.execucao_id "
                    "     AND n.no_id = x.no_id "
                    "LEFT JOIN usuarios u ON u.id = x.gerado_por "
                    "WHERE x.id = :id"
                ),
                {"id": exportacao_id},
            )
        )
        .mappings()
        .first()
    )
    if r is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Export not found.", status=404)
    det = dict(r)
    if det["tipo"] == "agrupado":
        det["partes"] = [
            dict(p)
            for p in (
                await conn.execute(
                    text(
                        "SELECT pt.ordem, pt.aba, pt.execucao_id, pt.no_id, pt.layout_id, "
                        "       pt.layout_versao, v.definicao, lp.nome AS layout_nome, "
                        "       e.pipeline_id, e.versao_numero, e.parametros, "
                        "       pi.nome AS pipeline_nome, n.snapshot, n.hash AS hash_snapshot, "
                        "       n.linhas AS linhas_snapshot "
                        "FROM relatorio_exportacao_partes pt "
                        "JOIN relatorio_layouts lp ON lp.id = pt.layout_id "
                        "JOIN relatorio_layout_versoes v ON v.layout_id = pt.layout_id "
                        "     AND v.numero = pt.layout_versao "
                        "JOIN execucoes e ON e.id = pt.execucao_id "
                        "JOIN pipelines pi ON pi.id = e.pipeline_id "
                        "JOIN execucao_nos n ON n.execucao_id = pt.execucao_id "
                        "     AND n.no_id = pt.no_id "
                        "WHERE pt.exportacao_id = :id ORDER BY pt.ordem"
                    ),
                    {"id": exportacao_id},
                )
            )
            .mappings()
            .all()
        ]
    return det


def _carimbo_agrupado(det: dict, momento: str) -> list[tuple[str, object]]:
    """Stamp of a grouped export: the artifact and, per sheet, the provenance of each part - the
    same a single-source stamp says, once per sheet."""
    quem = (
        det.get("gerado_por_nome") or det.get("gerado_por_email") or "platform (eager generation)"
    )
    linhas: list[tuple[str, object]] = [
        ("plataforma", "cFlow"),
        ("layout", det["layout_nome"]),
        ("layout_id", str(det["layout_id"])),
        ("layout_versao", det["layout_versao"]),
        ("formato", det["formato"]),
        ("cadeia_id", str(det["cadeia_id"]) if det.get("cadeia_id") else None),
        ("gerado_por", quem),
        ("gerado_em", momento),
        ("chave", det["chave"]),
    ]
    for p in det["partes"]:
        aba = p["aba"]
        linhas += [
            (f"{aba} · pipeline", p["pipeline_nome"]),
            (f"{aba} · pipeline_id", str(p["pipeline_id"])),
            (f"{aba} · versao_pipeline", p["versao_numero"]),
            (f"{aba} · execucao_id", str(p["execucao_id"])),
            (f"{aba} · no_id", p["no_id"]),
            (f"{aba} · hash_snapshot", p.get("hash_snapshot")),
            (f"{aba} · linhas_snapshot", p.get("linhas_snapshot")),
            (f"{aba} · layout", p["layout_nome"]),
            (f"{aba} · layout_id", str(p["layout_id"])),
            (f"{aba} · layout_versao", p["layout_versao"]),
        ]
    return linhas


def _carimbo(det: dict, momento: str) -> list[tuple[str, object]]:
    quem = (
        det.get("gerado_por_nome") or det.get("gerado_por_email") or "platform (eager generation)"
    )
    return [
        ("plataforma", "cFlow"),
        ("pipeline", det["pipeline_nome"]),
        ("pipeline_id", str(det["pipeline_id"])),
        ("versao_pipeline", det["versao_numero"]),
        ("execucao_id", str(det["execucao_id"])),
        ("no_id", det["no_id"]),
        ("hash_snapshot", det.get("hash_snapshot")),
        ("linhas_snapshot", det.get("linhas_snapshot")),
        ("layout", det["layout_nome"]),
        ("layout_id", str(det["layout_id"])),
        ("layout_versao", det["layout_versao"]),
        ("formato", det["formato"]),
        ("gerado_por", quem),
        ("gerado_em", momento),
        ("chave", det["chave"]),
    ]


async def _gravar_progresso(exportacao_id: uuid.UUID, linhas: int) -> None:
    async with transacao() as conn:
        await conn.execute(
            text("UPDATE relatorio_exportacoes SET linhas_escritas = :l WHERE id = :id"),
            {"l": linhas, "id": exportacao_id},
        )


async def gerar(exportacao_id: uuid.UUID | str, *, teto_linhas: int | None = None) -> dict:
    """Body of the activity (or of inline generation). Reads the node's Parquet, applies the
    layout, writes the IMMUTABLE artifact to the lake and closes the record as `pronto`. Any error
    closes it as `falha` with a user-facing message and does NOT raise: failure is a state, and the
    activity must not retry a file that will fail the same way (invalid data, missing column).
    Returns the record."""
    exportacao_id = uuid.UUID(str(exportacao_id))
    async with transacao() as conn:
        det = await _detalhe_para_gerar(conn, exportacao_id)
    if det["estado"] == "pronto":
        return await _obter_solo(exportacao_id)

    lake = await armazenamento.abrir(None)
    caminho = caminho_no_lake(det["chave"], det["formato"])
    try:
        if await lake.existe(CAMADA, caminho):
            # The tuple's artifact already exists (a previous attempt wrote it and died before
            # closing the record). Same click, same bytes: reuse it - regenerating would be a
            # second file in an immutable layer.
            dados = await lake.ler(CAMADA, caminho)
            snapshots_ = (
                [p["snapshot"] for p in det["partes"]]
                if det["tipo"] == "agrupado"
                else [det["snapshot"]]
            )
            linhas = sum(_gerar.linhas_do_parquet(s) for s in snapshots_)
            resultado = {"linhas": linhas, "bytes": len(dados), "abas": []}
        else:
            resultado = await _gerar_e_gravar(lake, det, caminho, teto_linhas)
        async with transacao() as conn:
            await conn.execute(
                text(
                    "UPDATE relatorio_exportacoes SET estado = 'pronto', linhas_total = :lt, "
                    "linhas_escritas = :le, bytes = :b, arquivo_ref = :ref, erro = NULL, "
                    "concluido_em = now() WHERE id = :id"
                ),
                {
                    "lt": resultado["linhas"],
                    "le": resultado["linhas"],
                    "b": resultado["bytes"],
                    "ref": lake.uri(CAMADA, caminho),
                    "id": exportacao_id,
                },
            )
        logger.info(
            "relatorio_exportacao_pronta",
            extra={
                "extra_campos": {
                    "exportacao_id": str(exportacao_id),
                    "linhas": resultado["linhas"],
                    "bytes": resultado["bytes"],
                }
            },
        )
    except Exception as exc:  # noqa: BLE001 - failure becomes STATE, with a message, never a loose exception
        mensagem = (
            exc.mensagem
            if isinstance(exc, ErroAplicacao)
            else f"Failed to generate the report ({type(exc).__name__})."
        )
        logger.exception(
            "relatorio_exportacao_falhou",
            extra={"extra_campos": {"exportacao_id": str(exportacao_id)}},
        )
        async with transacao() as conn:
            await conn.execute(
                text(
                    "UPDATE relatorio_exportacoes SET estado = 'falha', erro = :erro, "
                    "concluido_em = now() WHERE id = :id"
                ),
                {"erro": mensagem[:2000], "id": exportacao_id},
            )
    return await _obter_solo(exportacao_id)


async def _gerar_e_gravar(lake, det: dict, caminho: str, teto_linhas: int | None) -> dict:
    """Generate into a temporary file while a task records progress, and only then upload the bytes
    to the lake with O_EXCL - half a file never shows up as an artifact.

    Generation runs in a SEPARATE PROCESS (see `processo_xlsx`): the writer holds the GIL, and in
    a thread it competed with the node-execution process. A function replaced in memory (test,
    extension) does not exist in the child - that one runs in the thread instead."""
    momento = _gerar.momento_carimbo()
    kwargs = {} if teto_linhas is None else {"teto_linhas": teto_linhas}
    agrupado = det["tipo"] == "agrupado"
    if agrupado:
        funcao = "gerar_xlsx_agrupado"
        em_processo = _gerar.gerar_xlsx_agrupado is _GERAR_AGRUPADO_ORIGINAL
    else:
        funcao = "gerar_xlsx"
        em_processo = _gerar.gerar_xlsx is _GERAR_XLSX_ORIGINAL
    with tempfile.TemporaryDirectory(prefix="cflow_relatorio_") as tmp:
        destino = Path(tmp) / f"arquivo.{det['formato']}"
        arquivo_progresso = Path(tmp) / "progresso"
        progresso = {"linhas": 0}

        def _marcar(linhas: int) -> None:
            progresso["linhas"] = linhas

        def _lido() -> int:
            if (
                em_processo
            ):  # the child writes the count to a file; a closure cannot cross processes
                return _processo.ler_progresso(arquivo_progresso) or 0
            return progresso["linhas"]

        async def _pulsar() -> None:
            ultimo = 0
            while True:
                await asyncio.sleep(_PROGRESSO_SEGUNDOS)
                atual = _lido()
                if atual != ultimo:
                    ultimo = atual
                    try:
                        await _gravar_progresso(det["id"], atual)
                    except Exception:  # noqa: BLE001 - progress is cosmetic; generation continues
                        logger.warning("relatorio_progresso_falhou")

        if agrupado:
            # Each sheet uses its single-source layout AT THE PART'S VERSION and the parameters of
            # ITS OWN run - this makes the sheet come out identical to that source's own file.
            partes = [
                _gerar.ParteXlsx(p["snapshot"], p["definicao"], p["aba"], p.get("parametros") or {})
                for p in det["partes"]
            ]
            args = (partes, destino)
            opcoes = {
                "carimbo": _carimbo_agrupado(det, momento),
                "com_carimbo": bool(det["definicao"].get("carimbo", False)),
                **kwargs,
            }
        else:
            args = (det["snapshot"], det["definicao"], destino)
            opcoes = {
                "carimbo": _carimbo(det, momento),
                "parametros": det.get("parametros") or {},
                **kwargs,
            }
        pulso = asyncio.create_task(_pulsar())
        try:
            if em_processo:
                resultado = await _processo.gerar(
                    *args, progresso_em=str(arquivo_progresso), funcao=funcao, **opcoes
                )
            else:
                resultado = await asyncio.to_thread(
                    getattr(_gerar, funcao), *args, progresso=_marcar, **opcoes
                )
        finally:
            pulso.cancel()
        dados = await asyncio.to_thread(destino.read_bytes)
    await lake.gravar(CAMADA, caminho, dados, tipo_mime=MIME_XLSX)
    return resultado


async def _obter_solo(exportacao_id: uuid.UUID) -> dict:
    async with transacao() as conn:
        return await obter(conn, exportacao_id)


def _serializar(r: dict, partes: list[dict] | None = None) -> dict:
    return {
        "id": str(r["id"]),
        "chave": r["chave"],
        "tipo": r.get("tipo") or "simples",
        "execucao_id": str(r["execucao_id"]) if r.get("execucao_id") else None,
        "no_id": r["no_id"],
        "cadeia_id": str(r["cadeia_id"]) if r.get("cadeia_id") else None,
        # Grouped only: one entry per sheet, in file order.
        "partes": (
            [
                {
                    "aba": p["aba"],
                    "execucao_id": str(p["execucao_id"]),
                    "no_id": p["no_id"],
                    "layout_id": str(p["layout_id"]),
                    "layout_versao": p["layout_versao"],
                }
                for p in partes
            ]
            if partes is not None
            else None
        ),
        "layout_id": str(r["layout_id"]),
        "layout_versao": r["layout_versao"],
        "layout_nome": r.get("layout_nome"),
        "pipeline_id": str(r["pipeline_id"]) if r.get("pipeline_id") else None,
        "pipeline_nome": r.get("pipeline_nome"),
        "formato": r["formato"],
        "estado": r["estado"],
        "linhas_total": r["linhas_total"],
        "linhas_escritas": r["linhas_escritas"],
        "bytes": r["bytes"],
        "nome_arquivo": r["nome_arquivo"],
        "gerado_por": r.get("gerado_por_nome"),
        "criado_em": para_exibicao(r["criado_em"]).isoformat() if r["criado_em"] else None,
        "concluido_em": para_exibicao(r["concluido_em"]).isoformat() if r["concluido_em"] else None,
        "erro": r["erro"],
    }


_SELECT = (
    "SELECT x.*, l.nome AS layout_nome, e.pipeline_id, p.nome AS pipeline_nome, "
    "       u.nome AS gerado_por_nome "
    "FROM relatorio_exportacoes x "
    "JOIN relatorio_layouts l ON l.id = x.layout_id "
    "LEFT JOIN execucoes e ON e.id = x.execucao_id "
    "LEFT JOIN pipelines p ON p.id = e.pipeline_id "
    "LEFT JOIN usuarios u ON u.id = x.gerado_por "
)


async def _partes_de(conn: AsyncConnection, ids: list) -> dict[str, list[dict]]:
    """The parts of the requested grouped exports, in a single query (listing is not N+1)."""
    if not ids:
        return {}
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT exportacao_id, ordem, aba, execucao_id, no_id, layout_id, "
                    "layout_versao FROM relatorio_exportacao_partes "
                    "WHERE exportacao_id = ANY(:ids) ORDER BY exportacao_id, ordem"
                ),
                {"ids": list(ids)},
            )
        )
        .mappings()
        .all()
    )
    por: dict[str, list[dict]] = {}
    for r in rows:
        por.setdefault(str(r["exportacao_id"]), []).append(dict(r))
    return por


async def _serializar_linhas(conn: AsyncConnection, rows) -> list[dict]:
    agrupados = [r["id"] for r in rows if r.get("tipo") == "agrupado"]
    partes = await _partes_de(conn, agrupados)
    return [
        _serializar(r, partes.get(str(r["id"]), []) if r.get("tipo") == "agrupado" else None)
        for r in rows
    ]


async def obter(conn: AsyncConnection, exportacao_id: uuid.UUID) -> dict:
    r = (
        (await conn.execute(text(_SELECT + "WHERE x.id = :id"), {"id": exportacao_id}))
        .mappings()
        .first()
    )
    if r is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Export not found.", status=404)
    return (await _serializar_linhas(conn, [dict(r)]))[0]


async def listar(
    conn: AsyncConnection, *, execucao_id: uuid.UUID | None = None, limite: int = 100
) -> list[dict]:
    """History ("My reports"): most recent first; per run when requested - which also includes
    the grouped export that has a SHEET from that run."""
    sql = _SELECT
    params: dict = {"lim": max(1, min(int(limite), 500))}
    if execucao_id is not None:
        sql += (
            "WHERE x.execucao_id = :e OR EXISTS (SELECT 1 FROM relatorio_exportacao_partes pt "
            "WHERE pt.exportacao_id = x.id AND pt.execucao_id = :e) "
        )
        params["e"] = execucao_id
    rows = (
        (await conn.execute(text(sql + "ORDER BY x.criado_em DESC LIMIT :lim"), params))
        .mappings()
        .all()
    )
    return await _serializar_linhas(conn, [dict(r) for r in rows])


async def resumo_da_execucao(conn: AsyncConnection, execucao_id: uuid.UUID) -> dict:
    """`relatorios_prontos/total` - an additive signal for a run's status."""
    r = (
        (
            await conn.execute(
                text(
                    "SELECT count(*) FILTER (WHERE estado = 'pronto') AS prontos, "
                    "count(*) FILTER (WHERE estado <> 'falha') AS total, "
                    "count(*) FILTER (WHERE estado = 'falha') AS falhas "
                    "FROM relatorio_exportacoes WHERE execucao_id = :e"
                ),
                {"e": execucao_id},
            )
        )
        .mappings()
        .one()
    )
    return {"prontos": int(r["prontos"]), "total": int(r["total"]), "falhas": int(r["falhas"])}


async def arquivo(exportacao_id: uuid.UUID) -> dict:
    """Deliver the artifact as a local file for the API to stream (with session - never a signed
    URL: air-gapped). 409 if it is not ready yet."""
    async with transacao() as conn:
        det = await obter(conn, exportacao_id)
    if det["estado"] != "pronto":
        raise ErroAplicacao(
            codigo="exportacao_nao_pronta",
            mensagem=f"The export is '{det['estado']}'"
            + (f": {det['erro']}" if det["erro"] else "")
            + ".",
            status=409,
        )
    lake = await armazenamento.abrir(None)
    cache = Path(get_settings().lake_cache_dir) / CAMADA / det["chave"]
    local = await lake.materializar(
        CAMADA, caminho_no_lake(det["chave"], det["formato"]), cache / f"arquivo.{det['formato']}"
    )
    return {"caminho": local, "nome_arquivo": det["nome_arquivo"], "tipo_mime": MIME_XLSX}


# --- Eager generation -----------------------------------------------------------------------------


async def layouts_padrao_do_pipeline(conn: AsyncConnection, pipeline_id: uuid.UUID) -> list[dict]:
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT l.id, l.nome, v.numero, v.definicao->'fonte'->>'no' AS no_id "
                    "FROM relatorio_layouts l "
                    "JOIN relatorio_layout_versoes v ON v.id = l.versao_publicada_id "
                    "WHERE v.definicao->'fonte'->>'pipeline' = :p "
                    "  AND coalesce((v.definicao->>'padrao')::boolean, false) "
                    "ORDER BY l.nome"
                ),
                {"p": str(pipeline_id)},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def planejar_padroes(conn: AsyncConnection, execucao_id: uuid.UUID) -> list[dict]:
    """What eager generation will request for this run, already in ORDER: smallest snapshot first
    (Parquet bytes; `linhas` as tie-breaker). Only completed nodes with a snapshot."""
    execucao = await _execucao(conn, execucao_id)
    if execucao["status"] != "concluida":
        return []
    layouts = await layouts_padrao_do_pipeline(conn, execucao["pipeline_id"])
    if not layouts:
        return []
    nos = (
        (
            await conn.execute(
                text(
                    "SELECT no_id, linhas, snapshot FROM execucao_nos "
                    "WHERE execucao_id = :e AND status = 'concluida' AND snapshot IS NOT NULL"
                ),
                {"e": execucao_id},
            )
        )
        .mappings()
        .all()
    )
    por_no = {n["no_id"]: dict(n) for n in nos}
    plano = []
    for lay in layouts:
        no = por_no.get(lay["no_id"])
        if no is None:
            continue
        try:
            tamanho = os.path.getsize(no["snapshot"])
        except OSError:
            tamanho = -1
        plano.append(
            {
                "layout_id": lay["id"],
                "layout_nome": lay["nome"],
                "layout_versao": lay["numero"],
                "no_id": lay["no_id"],
                "bytes_snapshot": tamanho,
                "linhas": no["linhas"] or 0,
            }
        )
    plano.sort(key=lambda p: (p["bytes_snapshot"], p["linhas"], p["layout_nome"]))
    return plano


async def enfileirar_padroes(
    execucao_id: uuid.UUID | str, *, despachar: Despachador | None = None
) -> list[str]:
    """When a run completes: request every published default layout for its nodes, smallest
    snapshot first, off the critical path (the caller is `engine.finalizar`, after closing the
    run). A layout that does not check out (missing column) is logged and does NOT bring down the
    others - nor the run, which has already completed. Returns the created ids."""
    execucao_id = uuid.UUID(str(execucao_id))
    async with transacao() as conn:
        plano = await planejar_padroes(conn, execucao_id)
    criadas: list[str] = []
    for item in plano:
        try:
            res = await solicitar(
                execucao_id=execucao_id,
                no_id=item["no_id"],
                layout_id=uuid.UUID(str(item["layout_id"])),
                ator_id=None,
                despachar=despachar,
            )
        except ErroAplicacao as exc:
            logger.warning(
                "relatorio_padrao_nao_enfileirado",
                extra={
                    "extra_campos": {
                        "execucao_id": str(execucao_id),
                        "layout": item["layout_nome"],
                        "codigo": exc.codigo,
                    }
                },
            )
            continue
        if res["criada"]:
            criadas.append(res["exportacao"]["id"])
    criadas += await enfileirar_agrupados(execucao_id, despachar=despachar)
    if criadas:
        logger.info(
            "relatorios_padrao_enfileirados",
            extra={"extra_campos": {"execucao_id": str(execucao_id), "quantos": len(criadas)}},
        )
    return criadas


async def planejar_agrupados(conn: AsyncConnection, execucao_id: uuid.UUID) -> list[dict]:
    """The DEFAULT grouped exports that this run completes: each one whose sheets point at layouts
    of its pipeline and whose OTHER parts already have a COMPLETED run in the SAME chain
    (`cadeia_id`).

    Independent of completion order: each run that completes checks, after writing its own status,
    whether its siblings have completed - the last one to complete sees all of them (if two
    complete together, both may see it, and the unique key makes both reach the SAME record). A
    part with no completed run yet -> the grouped export waits until it completes. Outside a chain
    (`cadeia_id` null) only a grouped layout whose sheets all belong to this pipeline is completed.

    Returns `[{layout_id, layout_nome, partes: {sheet: execucao_id}}]`."""
    execucao = await _execucao(conn, execucao_id)
    if execucao["status"] != "concluida":
        return []
    agrupados = (
        (
            await conn.execute(
                text(
                    "SELECT l.id, l.nome, v.definicao FROM relatorio_layouts l "
                    "JOIN relatorio_layout_versoes v ON v.id = l.versao_publicada_id "
                    "WHERE v.definicao->>'tipo' = 'agrupado' "
                    "  AND coalesce((v.definicao->>'padrao')::boolean, false) "
                    "ORDER BY l.nome"
                )
            )
        )
        .mappings()
        .all()
    )
    if not agrupados:
        return []
    ids = sorted({lid for a in agrupados for _, lid in _layout.abas_do_agrupado(a["definicao"])})
    partes_pub = await _layouts_publicados(conn, ids)
    pipeline = str(execucao["pipeline_id"])
    cadeia = execucao["cadeia_id"]
    planos = []
    for ag in agrupados:
        abas = _layout.abas_do_agrupado(ag["definicao"])
        pipeline_da_aba: dict[str, str] = {}
        for aba, lid in abas:
            d = (partes_pub.get(lid) or {}).get("definicao")
            if d and not _layout.eh_agrupado(d):
                pipeline_da_aba[aba] = str((d.get("fonte") or {}).get("pipeline"))
        if len(pipeline_da_aba) != len(abas) or pipeline not in pipeline_da_aba.values():
            continue  # sheet with no published layout (becomes a named error on request) or not ours
        partes: dict[str, str] = {}
        for aba, _ in abas:
            pid = pipeline_da_aba[aba]
            if pid == pipeline:
                partes[aba] = str(execucao_id)
                continue
            if cadeia is None:
                break
            irma = (
                await conn.execute(
                    text(
                        "SELECT id FROM execucoes WHERE cadeia_id = :c AND pipeline_id = :p "
                        "AND status = 'concluida' ORDER BY concluida_em DESC LIMIT 1"
                    ),
                    {"c": cadeia, "p": uuid.UUID(pid)},
                )
            ).scalar_one_or_none()
            if irma is None:
                break
            partes[aba] = str(irma)
        if len(partes) == len(abas):
            planos.append({"layout_id": ag["id"], "layout_nome": ag["nome"], "partes": partes})
    return planos


async def enfileirar_agrupados(
    execucao_id: uuid.UUID | str, *, despachar: Despachador | None = None
) -> list[str]:
    """Request the default grouped exports that this run completed (`planejar_agrupados`). As in
    single-source eager mode: whatever does not check out is logged, never raised - the run has
    already completed."""
    execucao_id = uuid.UUID(str(execucao_id))
    async with transacao() as conn:
        planos = await planejar_agrupados(conn, execucao_id)
    criadas: list[str] = []
    for plano in planos:
        try:
            res = await solicitar_agrupado(
                layout_id=uuid.UUID(str(plano["layout_id"])),
                partes=plano["partes"],
                ator_id=None,
                despachar=despachar,
            )
        except ErroAplicacao as exc:
            logger.warning(
                "relatorio_agrupado_nao_enfileirado",
                extra={
                    "extra_campos": {
                        "execucao_id": str(execucao_id),
                        "layout": plano["layout_nome"],
                        "codigo": exc.codigo,
                    }
                },
            )
            continue
        if res["criada"]:
            criadas.append(res["exportacao"]["id"])
    return criadas
