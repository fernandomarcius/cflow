"""Attachments of the `email` node: turns data into a FILE (bytes), with no knowledge of SMTP or the graph.

Two sources, chosen in the node editor:
- `dados`:  a node result becomes a file here (csv/xlsx/json/parquet), with a header.
- `coluna`: the previous node ALREADY built the file; we only extract name + content from the row.

Parquet is the only format not serialised here: the data already sits in a DuckDB table on both
paths (preview and execution), so the caller injects the exporter (`parquet_bytes`). This keeps the
module pure and the Parquet comes out of the SAME engine that writes the snapshots.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
from collections.abc import Callable

from ..conexoes.conectores.base import AnexoEmail
from ..errors import ErroAplicacao
from .email_template import texto_valor

FORMATOS = ("csv", "xlsx", "json", "parquet")
CODIFICACOES = ("utf-8-sig", "utf-8", "latin-1")
CODIFICACOES_CONTEUDO = ("base64", "hex", "texto")

MIME_POR_EXTENSAO = {
    "csv": "text/csv",
    "txt": "text/plain",
    "json": "application/json",
    "xml": "application/xml",
    "html": "text/html",
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "zip": "application/zip",
    "parquet": "application/octet-stream",
}


def tipo_mime(nome: str) -> str:
    ext = nome.rsplit(".", 1)[-1].lower() if "." in nome else ""
    return MIME_POR_EXTENSAO.get(ext, "application/octet-stream")


def nome_seguro(nome: str) -> str:
    """File name without a path: the name may come from a placeholder/column, and `../` or `/` in an
    attachment is not just ugly: whoever receives and saves it would write outside the intended
    directory."""
    limpo = (
        (nome or "").replace("\\", "/").split("/")[-1].strip().replace("\r", "").replace("\n", "")
    )
    limpo = limpo.lstrip(".") or "anexo"
    return limpo[:180]


def _csv_bytes(
    colunas: list[str],
    linhas: list[dict],
    *,
    delimitador: str,
    incluir_cabecalho: bool,
    codificacao: str,
) -> bytes:
    buf = io.StringIO(newline="")
    # `\r\n` + `utf-8-sig`: the pair that makes Excel open accents correctly without merging rows.
    escritor = csv.writer(buf, delimiter=(delimitador or ";")[:1], lineterminator="\r\n")
    if incluir_cabecalho:
        escritor.writerow(colunas)
    for linha in linhas:
        escritor.writerow([texto_valor(linha.get(c)) for c in colunas])
    return buf.getvalue().encode(codificacao if codificacao in CODIFICACOES else "utf-8-sig")


def _xlsx_bytes(colunas: list[str], linhas: list[dict], aba: str) -> bytes:
    from openpyxl import Workbook

    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title=(aba or "Dados")[:31])
    ws.append(colunas)
    for linha in linhas:
        ws.append([texto_valor(linha.get(c)) for c in colunas])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _json_bytes(colunas: list[str], linhas: list[dict]) -> bytes:
    dados = [{c: linha.get(c) for c in colunas} for linha in linhas]
    return json.dumps(dados, ensure_ascii=False, default=str, indent=2).encode("utf-8")


def serializar(
    formato: str,
    colunas: list[str],
    linhas: list[dict],
    *,
    delimitador: str = ";",
    incluir_cabecalho: bool = True,
    codificacao: str = "utf-8-sig",
    aba: str = "Dados",
    parquet_bytes: Callable[[], bytes] | None = None,
) -> bytes:
    if formato == "csv":
        return _csv_bytes(
            colunas,
            linhas,
            delimitador=delimitador,
            incluir_cabecalho=incluir_cabecalho,
            codificacao=codificacao,
        )
    if formato == "xlsx":
        return _xlsx_bytes(colunas, linhas, aba)
    if formato == "json":
        return _json_bytes(colunas, linhas)
    if formato == "parquet":
        if parquet_bytes is None:
            raise ErroAplicacao(
                codigo="email_anexo_formato",
                mensagem="Parquet attachment is unavailable in this context.",
                status=400,
            )
        return parquet_bytes()
    raise ErroAplicacao(
        codigo="email_anexo_formato",
        mensagem=f"Attachment format '{formato}' is not supported (use {', '.join(FORMATOS)}).",
        status=400,
    )


def de_dados(
    cfg: dict,
    nome: str,
    colunas: list[str],
    linhas: list[dict],
    *,
    parquet_bytes: Callable[[], bytes] | None = None,
) -> AnexoEmail:
    """Build the attachment from a node result. 0 rows with `permitir_vazio=false` FAILS: a
    header-only attachment is a report that looks delivered but is not."""
    formato = cfg.get("formato") or "csv"
    if not linhas and not cfg.get("permitir_vazio", False):
        raise ErroAplicacao(
            codigo="email_anexo_vazio",
            mensagem=f"Attachment '{nome}': 0 rows and permitir_vazio=false.",
            status=400,
        )
    conteudo = serializar(
        formato,
        colunas,
        linhas,
        delimitador=cfg.get("delimitador") or ";",
        incluir_cabecalho=cfg.get("incluir_cabecalho", True),
        codificacao=cfg.get("codificacao") or "utf-8-sig",
        aba=cfg.get("aba") or "Dados",
        parquet_bytes=parquet_bytes,
    )
    seguro = nome_seguro(nome)
    return AnexoEmail(
        nome=seguro, conteudo=conteudo, tipo_mime=cfg.get("tipo_mime") or tipo_mime(seguro)
    )


def _decodificar(valor: object, codificacao: str, nome: str) -> bytes:
    if isinstance(valor, (bytes, bytearray, memoryview)):
        return bytes(valor)
    texto = "" if valor is None else str(valor)
    try:
        if codificacao == "base64":
            return base64.b64decode(texto, validate=True)
        if codificacao == "hex":
            return bytes.fromhex(texto)
    except (binascii.Error, ValueError) as exc:
        raise ErroAplicacao(
            codigo="email_anexo_conteudo",
            mensagem=f"Attachment '{nome}': content is not valid {codificacao}.",
            status=400,
        ) from exc
    return texto.encode("utf-8")


def de_coluna(cfg: dict, linha: dict) -> AnexoEmail:
    """Attachment whose FILE already came ready from the previous node (content in a column).

    Accepts raw `bytes` (code node/BLOB) or encoded text: DuckDB returns `BLOB` as bytes, but a
    third-party Parquet usually carries base64 in a text column."""
    col_conteudo = cfg.get("coluna_conteudo")
    if not col_conteudo:
        raise ErroAplicacao(
            codigo="email_anexo_coluna",
            mensagem="Column attachment requires 'coluna_conteudo'.",
            status=400,
        )
    if col_conteudo not in linha:
        raise ErroAplicacao(
            codigo="email_anexo_coluna",
            mensagem=f"Column attachment: column '{col_conteudo}' does not exist in the input.",
            status=400,
        )
    col_nome = cfg.get("coluna_nome")
    if col_nome and col_nome not in linha:
        raise ErroAplicacao(
            codigo="email_anexo_coluna",
            mensagem=f"Column attachment: column '{col_nome}' does not exist in the input.",
            status=400,
        )
    nome = nome_seguro(str(linha.get(col_nome) or "") if col_nome else (cfg.get("nome") or "anexo"))
    codificacao = cfg.get("codificacao_conteudo") or "base64"
    if codificacao not in CODIFICACOES_CONTEUDO:
        raise ErroAplicacao(
            codigo="email_anexo_conteudo",
            mensagem=f"Invalid 'codificacao_conteudo' (use {', '.join(CODIFICACOES_CONTEUDO)}).",
            status=400,
        )
    conteudo = _decodificar(linha.get(col_conteudo), codificacao, nome)
    if not conteudo and not cfg.get("permitir_vazio", False):
        raise ErroAplicacao(
            codigo="email_anexo_vazio",
            mensagem=f"Attachment '{nome}': empty content and permitir_vazio=false.",
            status=400,
        )
    return AnexoEmail(
        nome=nome, conteudo=conteudo, tipo_mime=cfg.get("tipo_mime") or tipo_mime(nome)
    )
