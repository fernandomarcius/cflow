"""Landing -> bronze: decompress, decode with the DECLARED encoding and materialize Parquet.

Two rules that hold for every bronze dataset:

1. **Encoding comes from the contract, never inferred.** Sources may be `latin-1`, `cp1252`, etc.
   Guessing is right for most rows and wrong for the one with an accented character - and the
   error only shows up in a report, months later.
2. **Bronze is all text (`VARCHAR`).** Inferring types here eats leading zeros from identifiers,
   turns date strings into timestamps with a timezone and turns `0,00` into `0`. Typing is a
   business decision; it happens later, in the pipeline, where it is visible and versioned.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import re
import tempfile
import zipfile
from pathlib import Path

from ..duck import conectar as conectar_duck
from ..errors import ErroAplicacao


class ErroNormalizacao(ErroAplicacao):
    """Decoding/parse/validation failure - what sends the batch to QUARANTINE (not an infra
    failure: re-running does not fix it, someone has to look)."""

    def __init__(self, codigo: str, mensagem: str, etapa: str = "parse") -> None:
        super().__init__(codigo=codigo, mensagem=mensagem, status=422)
        self.etapa = etapa


def _lit(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _descomprimir(dados: bytes, fmt: dict, nome_base: str) -> list[tuple[str, bytes]]:
    compressao = fmt.get("compressao")
    if not compressao:
        return [(nome_base, dados)]
    if compressao == "gzip":
        try:
            return [(nome_base.removesuffix(".gz"), gzip.decompress(dados))]
        except OSError as e:
            raise ErroNormalizacao(
                "gzip_invalido", f"Unreadable gzip file: {e}.", "decodificacao"
            ) from None
    try:
        zf = zipfile.ZipFile(io.BytesIO(dados))
    except zipfile.BadZipFile:
        raise ErroNormalizacao(
            "zip_invalido",
            "The downloaded file is not a valid ZIP (did the origin return an error page?).",
            "decodificacao",
        ) from None
    padrao = re.compile(fmt["arquivo_no_zip"], re.IGNORECASE) if fmt.get("arquivo_no_zip") else None
    membros = [
        i
        for i in zf.infolist()
        if not i.is_dir() and (padrao.search(i.filename) if padrao else True)
    ]
    if not membros:
        raise ErroNormalizacao(
            "zip_sem_membro",
            "No file inside the ZIP matches 'formato.arquivo_no_zip'.",
            "decodificacao",
        )
    ordenados = sorted(membros, key=lambda m: m.filename)
    return [(m.filename.rsplit("/", 1)[-1], zf.read(m)) for m in ordenados]


def _decodificar(bruto: bytes, encoding: str, *, onde: str) -> str:
    try:
        return bruto.decode(encoding)
    except (UnicodeDecodeError, LookupError) as e:
        raise ErroNormalizacao(
            "encoding_incompativel",
            f"'{onde}' does not decode as '{encoding}': {e}. "
            "Check 'formato.encoding' in the source contract.",
            "decodificacao",
        ) from None


def _recortar_secao(texto: str, fmt: dict) -> str:
    """`formato.prefixo_linha`: keeps only the lines that start with the prefix and REMOVES it.

    Exists for single-file reports that glue two different tables together (e.g. `1@` totals,
    `2@` detail), with different column counts. Without the cut, either the parse breaks on the
    ragged line or - worse - passes with `null_padding` and bronze becomes two stacked tables
    pretending to be one.
    """
    prefixo = fmt.get("prefixo_linha")
    if not prefixo:
        return texto
    linhas = [ln[len(prefixo) :] for ln in texto.splitlines() if ln.startswith(prefixo)]
    if not linhas:
        raise ErroNormalizacao(
            "prefixo_sem_linha",
            f"No line starts with the prefix '{prefixo}' declared in the contract.",
            "validacao",
        )
    return "\n".join(linhas)


def _linhas_largura_fixa(texto: str, fmt: dict) -> list[dict]:
    campos = fmt["campos"]
    pular = int(fmt.get("pular_linhas", 0) or 0)
    descartar_final = int(fmt.get("descartar_ultimas", 0) or 0)
    linhas = texto.splitlines()
    if pular:
        linhas = linhas[pular:]
    if descartar_final:
        linhas = linhas[: len(linhas) - descartar_final] if descartar_final < len(linhas) else []
    largura_min = max(c["inicio"] + c["tamanho"] - 1 for c in campos)
    out: list[dict] = []
    for n, linha in enumerate(linhas, start=1):
        if not linha.strip():
            continue
        if len(linha) < largura_min:
            raise ErroNormalizacao(
                "largura_fixa_curta",
                f"Line {n} has {len(linha)} characters; the layout requires at least {largura_min}.",
            )
        out.append(
            {
                c["nome"]: linha[c["inicio"] - 1 : c["inicio"] - 1 + c["tamanho"]].strip()
                for c in campos
            }
        )
    return out


def _itens_json(texto: str, fmt: dict) -> list[dict]:
    try:
        corpo = json.loads(texto)
    except ValueError as e:
        raise ErroNormalizacao("json_invalido", f"Content is not valid JSON: {e}.") from None
    caminho = fmt.get("json_path")
    no = corpo
    for parte in caminho.split(".") if caminho else []:
        no = no.get(parte) if isinstance(no, dict) else None
    # Without `json_path` (or with one that did not resolve), the record list is the ONLY key that
    # is a list - the OData pattern (`value`), which the connector wraps together with
    # `_paginas`/`_truncado`. Without this lookup the whole envelope became ONE row with the columns
    # `value`/`_paginas`, and `schema_esperado` failed saying the real column "does not exist" - a
    # message that sends whoever debugs to investigate the origin, when the defect was here.
    if (no is None or (not caminho and isinstance(no, dict))) and isinstance(corpo, dict):
        listas = [v for v in corpo.values() if isinstance(v, list)]
        if len(listas) == 1:
            no = listas[0]
    if no is None:
        no = corpo
    if isinstance(no, dict):
        no = [no]
    if not isinstance(no, list):
        raise ErroNormalizacao("json_sem_lista", "The JSON does not contain a list of records.")
    return no


def _ndjson(registros: list[dict], destino: Path) -> list[str]:
    """Writes the records as NDJSON of STRINGS (bronze is all text). A nested value becomes JSON
    text - the data is not lost, and the column stays VARCHAR like all the others."""
    colunas: list[str] = []
    vistas: set[str] = set()
    with open(destino, "w", encoding="utf-8") as fp:
        for reg in registros:
            if not isinstance(reg, dict):
                reg = {"valor": reg}
            linha = {}
            for k, v in reg.items():
                nome = str(k)
                if nome not in vistas:
                    vistas.add(nome)
                    colunas.append(nome)
                if v is None:
                    linha[nome] = None
                elif isinstance(v, (dict, list)):
                    linha[nome] = json.dumps(v, ensure_ascii=False)
                else:
                    linha[nome] = str(v)
            fp.write(json.dumps(linha, ensure_ascii=False) + "\n")
    return colunas


def _select(caminho: Path, fmt: dict, tipo_arquivo: str) -> str:
    if tipo_arquivo == "csv":
        delim = fmt.get("delimitador", ",")
        partes = [
            _lit(str(caminho)),
            f"delim={_lit(delim)}",
            "header=true",
            "all_varchar=true",
            "sample_size=-1",
        ]
        if fmt.get("pular_linhas"):
            partes.append(f"skip={int(fmt['pular_linhas'])}")
        if fmt.get("tolerar_linhas_invalidas"):
            # `store_rejects`, never `ignore_errors`: both let the load pass, but only this one keeps
            # WHAT was discarded, in `reject_errors`. Silent discard is exactly what quarantine
            # exists to prevent - the tolerance trades "I stop" for "I go on and tell you how
            # many", not for "I hide it".
            partes.append("store_rejects=true")
        return f"SELECT * FROM read_csv({', '.join(partes)})"
    if tipo_arquivo == "ndjson":
        return (
            f"SELECT * FROM read_json({_lit(str(caminho))}, "
            "format='newline_delimited', records=true)"
        )
    return f"SELECT * FROM read_parquet({_lit(str(caminho))})"


def _diagnosticar_csv(membros: list[tuple[str, bytes]], fmt: dict) -> str | None:
    """Points at the first line whose field count differs from the header.

    DuckDB fails with "Error when sniffing file /tmp/cflow_bronze_xxx/parte_0.csv" - which says
    neither which origin file nor where. This is third-party data and really arrives malformed: a
    single row may have the delimiter inside an unquoted field. Without pointing at the line,
    finding it is a manual hunt inside a zip.
    """
    delim = fmt.get("delimitador", ",")
    encoding = fmt.get("encoding") or "utf-8"
    for nome, bruto in membros:
        try:
            linhas = [ln for ln in bruto.decode(encoding).splitlines() if ln.strip()]
        except (UnicodeDecodeError, LookupError):
            continue
        if not linhas:
            continue
        esperado = linhas[0].count(delim) + 1
        for n, linha in enumerate(linhas[1:], start=2):
            achado = linha.count(delim) + 1
            if achado != esperado:
                return (
                    f"'{nome}' line {n}: {achado} fields, the header declares {esperado} "
                    f"(delimiter '{delim}' inside an unquoted value?). Excerpt: "
                    f"{linha[:120]!r}"
                )
    return None


def _exigir_layout_unico(membros: list[tuple[str, bytes]], selects: list[str]) -> None:
    """Refuses a bundle ZIP: several members with different LAYOUTS are not a single table.

    Some publishers ship a zip with many csv files of distinct grains. Stacking them with
    `UNION ALL BY NAME` "works": it yields a very wide Parquet, mostly null per row, that lies about
    its own grain and is only discovered by whoever queries it. Failing here, naming the members,
    is the honest outcome - and the fix is to declare `arquivo_no_zip` and register one source per
    table.
    """
    import duckdb

    if len(selects) < 2:
        return
    con = conectar_duck("bronze")
    try:
        colunas: list[tuple[str, frozenset]] = []
        for (nome, _), select in zip(membros, selects, strict=True):
            cur = con.execute(f"SELECT * FROM ({select}) LIMIT 0")
            colunas.append((nome, frozenset(d[0] for d in cur.description)))
    except duckdb.Error:
        return  # the real error shows up at parse time; this is no place to mask it
    finally:
        con.close()

    distintos = {c for _, c in colunas}
    if len(distintos) > 1:
        exemplos = ", ".join(nome for nome, _ in colunas[:4])
        raise ErroNormalizacao(
            "zip_multitabela",
            f"The file has {len(colunas)} tables with different layouts ({exemplos}"
            f"{'...' if len(colunas) > 4 else ''}). Declare 'formato.arquivo_no_zip' and register one "
            "source per table - merging them into a single Parquet would produce a dataset with no "
            "defined grain.",
            "validacao",
        )


def _normalizar_sync(
    dados: bytes, contrato: dict, diagnostico: dict | None = None
) -> tuple[bytes, int, list[str]]:
    import duckdb

    fmt = contrato["formato"]
    tipo = fmt["tipo"]
    encoding = fmt.get("encoding") or "utf-8"
    membros = _descomprimir(dados, fmt, f"{contrato['dataset']}.dados")

    with tempfile.TemporaryDirectory(prefix="cflow_bronze_") as tmp:
        base = Path(tmp)
        selects: list[str] = []
        for i, (nome, bruto) in enumerate(membros):
            if tipo == "parquet":
                alvo = base / f"parte_{i}.parquet"
                alvo.write_bytes(bruto)
                selects.append(_select(alvo, fmt, "parquet"))
                continue
            texto = _recortar_secao(_decodificar(bruto, encoding, onde=nome), fmt)
            if tipo == "csv":
                alvo = base / f"parte_{i}.csv"
                alvo.write_text(texto, encoding="utf-8")
                selects.append(_select(alvo, fmt, "csv"))
            else:  # json | largura_fixa -> NDJSON of strings
                registros = (
                    _itens_json(texto, fmt) if tipo == "json" else _linhas_largura_fixa(texto, fmt)
                )
                if not registros:
                    raise ErroNormalizacao(
                        "lote_sem_registros",
                        "The batch has no records after parsing "
                        "(has the slice not been published yet?).",
                        "validacao",
                    )
                alvo = base / f"parte_{i}.ndjson"
                _ndjson(registros, alvo)
                selects.append(_select(alvo, fmt, "ndjson"))

        # UNION ALL BY NAME: a ZIP with several members of the same layout becomes ONE table, even
        # if the column order changes between members (happens in historical series). But only when
        # the LAYOUT is the same - see `_exigir_layout_unico`.
        _exigir_layout_unico(membros, selects)
        consulta = " UNION ALL BY NAME ".join(f"({s})" for s in selects)
        saida = base / "dados.parquet"
        con = conectar_duck("bronze")
        try:
            try:
                cur = con.execute(f"SELECT * FROM ({consulta}) LIMIT 0")
                colunas = [d[0] for d in cur.description]
            except duckdb.Error as e:
                detalhe = _diagnosticar_csv(membros, fmt) if tipo == "csv" else None
                raise ErroNormalizacao(
                    "parse_falhou",
                    f"Failed to read the content: {detalhe or e}.",
                ) from None

            faltando = [c for c in (contrato.get("schema_esperado") or []) if c not in colunas]
            if faltando:
                # A broken schema is QUARANTINE, not crooked bronze: the origin changed its layout and
                # consumers deserve to find out now, not from a missing column months later.
                raise ErroNormalizacao(
                    "schema_inesperado",
                    "Expected columns missing: " + ", ".join(faltando) + ".",
                    "validacao",
                )
            try:
                con.execute(
                    f"COPY ({consulta}) TO {_lit(str(saida))} (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
                linhas = con.execute(
                    f"SELECT count(*) FROM read_parquet({_lit(str(saida))})"
                ).fetchone()[0]
            except duckdb.Error as e:
                raise ErroNormalizacao(
                    "parse_falhou", f"Failed to materialize the Parquet: {e}."
                ) from None
            if diagnostico is not None and fmt.get("tolerar_linhas_invalidas"):
                diagnostico.update(_rejeicoes(con))
        finally:
            con.close()
        return saida.read_bytes(), int(linhas), colunas


def _rejeicoes(con) -> dict:
    """What `store_rejects` discarded, for the manifest.

    Without this the tolerance would be indistinguishable from `ignore_errors`: the load passes and
    nobody knows what was left out. The line and the reason go into the manifest, which answers
    "where did this number come from?" for whoever only has the Parquet."""
    import duckdb

    try:
        linhas = con.execute(
            "SELECT DISTINCT line, error_type, error_message FROM reject_errors ORDER BY line"
        ).fetchall()
    except duckdb.Error:
        return {}  # no rejections: DuckDB does not even create the table
    if not linhas:
        return {}
    return {
        "linhas_rejeitadas": len(linhas),
        "rejeicoes": [
            {"linha": int(ln), "tipo": str(tp), "mensagem": str(msg)[:200]}
            for ln, tp, msg in linhas[:20]
        ],
    }


async def normalizar(
    dados: bytes, contrato: dict, *, diagnostico: dict | None = None
) -> tuple[bytes, int, list[str]]:
    """`(parquet, linhas, colunas)` from the raw landing payload. DuckDB is synchronous - it runs
    in a thread so it does not block the worker event loop.

    `diagnostico` is filled in (not returned) with what the tolerance discarded. It is passed as a
    parameter because only a couple of the many callers care, and widening the tuple would force
    the others to unpack a value they ignore."""
    return await asyncio.to_thread(_normalizar_sync, dados, contrato, diagnostico)
