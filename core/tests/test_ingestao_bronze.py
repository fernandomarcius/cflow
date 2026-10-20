"""Landing -> bronze: decompression, declared encoding, parsing and schema validation.

The case this file exists to lock down: **accents**. Sources deliver `latin-1` or `cp1252`;
decoding as UTF-8 does not blow up on most rows - it corrupts the one with a `Ç` and becomes an
audit finding months later. Here the wrong encoding must FAIL, and the right one must preserve.
"""

from __future__ import annotations

import io
import json
import zipfile

import duckdb
import pytest

from cflow_core.ingestao.bronze import ErroNormalizacao, normalizar

CSV_LATIN1 = "ENTITY_ID;REF_DATE;PRICE;NAME\n000-001;2026-08-01;1,2345;CAFÉ CRÈME Ç\n"


def _zip(nome: str, conteudo: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(nome, conteudo)
    return buf.getvalue()


def _ler(parquet: bytes, tmp_path) -> list[dict]:
    alvo = tmp_path / "x.parquet"
    alvo.write_bytes(parquet)
    con = duckdb.connect()
    try:
        cur = con.execute(f"SELECT * FROM read_parquet('{alvo}')")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, linha, strict=True)) for linha in cur.fetchall()]
    finally:
        con.close()


def _contrato_csv(**over) -> dict:
    c = {
        "classe": "http_file",
        "dataset": "registry_monthly",
        "janela": {"tipo": "mensal"},
        "acesso": {"url_padrao": "https://example.com/{ano}{mes}.zip"},
        "formato": {
            "tipo": "csv",
            "encoding": "latin-1",
            "delimitador": ";",
            "compressao": "zip",
            "arquivo_no_zip": r"\.csv$",
        },
        "schema_esperado": ["ENTITY_ID", "REF_DATE", "PRICE"],
    }
    c.update(over)
    return c


async def test_csv_latin1_em_zip_preserva_acento(tmp_path) -> None:
    dados = _zip("inf.csv", CSV_LATIN1.encode("latin-1"))
    parquet, linhas, colunas = await normalizar(dados, _contrato_csv())
    assert linhas == 1
    assert colunas == ["ENTITY_ID", "REF_DATE", "PRICE", "NAME"]
    assert _ler(parquet, tmp_path)[0]["NAME"] == "CAFÉ CRÈME Ç"


async def test_bronze_e_todo_texto(tmp_path) -> None:
    """Inferring types here eats leading zeros and turns `1,2345` into a number (or an error)."""
    parquet, _, _ = await normalizar(_zip("inf.csv", CSV_LATIN1.encode("latin-1")), _contrato_csv())
    linha = _ler(parquet, tmp_path)[0]
    assert linha["PRICE"] == "1,2345"  # string, with the origin's comma
    assert linha["ENTITY_ID"] == "000-001"
    assert isinstance(linha["ENTITY_ID"], str)


async def test_encoding_errado_vai_para_quarentena() -> None:
    contrato = _contrato_csv()
    contrato["formato"] = {**contrato["formato"], "encoding": "ascii"}
    with pytest.raises(ErroNormalizacao) as e:
        await normalizar(_zip("inf.csv", CSV_LATIN1.encode("latin-1")), contrato)
    assert e.value.codigo == "encoding_incompativel"
    assert e.value.etapa == "decodificacao"


async def test_schema_faltando_vai_para_quarentena() -> None:
    """Origin changed the layout: quarantine with a name, not a bronze with a missing column."""
    contrato = _contrato_csv(schema_esperado=["ENTITY_ID", "COLUNA_QUE_SUMIU"])
    with pytest.raises(ErroNormalizacao) as e:
        await normalizar(_zip("inf.csv", CSV_LATIN1.encode("latin-1")), contrato)
    assert e.value.codigo == "schema_inesperado"
    assert "COLUNA_QUE_SUMIU" in e.value.mensagem


async def test_zip_invalido_e_erro_nomeado() -> None:
    with pytest.raises(ErroNormalizacao) as e:
        await normalizar(b"<html>error 500</html>", _contrato_csv())
    assert e.value.codigo == "zip_invalido"


async def test_zip_sem_membro_que_case() -> None:
    contrato = _contrato_csv()
    contrato["formato"] = {**contrato["formato"], "arquivo_no_zip": r"\.txt$"}
    with pytest.raises(ErroNormalizacao) as e:
        await normalizar(_zip("inf.csv", CSV_LATIN1.encode("latin-1")), contrato)
    assert e.value.codigo == "zip_sem_membro"


async def test_varios_membros_do_zip_viram_uma_tabela(tmp_path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.csv", CSV_LATIN1.encode("latin-1"))
        zf.writestr("b.csv", CSV_LATIN1.encode("latin-1"))
    parquet, linhas, _ = await normalizar(buf.getvalue(), _contrato_csv())
    assert linhas == 2


async def test_json_de_lista_simples(tmp_path) -> None:
    contrato = {
        "classe": "rest_api",
        "dataset": "rates_daily",
        "janela": {"tipo": "periodo"},
        "acesso": {"url_padrao": "https://example.com"},
        "formato": {"tipo": "json"},
        "schema_esperado": ["data", "valor"],
    }
    corpo = json.dumps([{"data": "01/08/2026", "valor": "0.052531"}]).encode()
    parquet, linhas, colunas = await normalizar(corpo, contrato)
    assert linhas == 1 and colunas == ["data", "valor"]
    assert _ler(parquet, tmp_path)[0]["valor"] == "0.052531"


async def test_json_do_odata_acha_a_lista_value(tmp_path) -> None:
    contrato = {
        "classe": "odata",
        "dataset": "quotes_monthly",
        "janela": {"tipo": "mensal"},
        "acesso": {"url_padrao": "https://example.com"},
        "formato": {"tipo": "json", "json_path": "value"},
    }
    corpo = json.dumps({"value": [{"bidPrice": 5.12}], "_paginas": 1}).encode()
    parquet, linhas, _ = await normalizar(corpo, contrato)
    assert linhas == 1
    assert _ler(parquet, tmp_path)[0]["bidPrice"] == "5.12"


async def test_largura_fixa_cp1252(tmp_path) -> None:
    """Fixed-width layout: `inicio` is 1-based - converting in your head is where mistakes happen."""
    contrato = {
        "classe": "http_file",
        "dataset": "trades_yearly",
        "janela": {"tipo": "anual"},
        "acesso": {"url_padrao": "https://example.com/{ano}.zip"},
        "formato": {
            "tipo": "largura_fixa",
            "encoding": "cp1252",
            "campos": [
                {"nome": "REC_TYPE", "inicio": 1, "tamanho": 2},
                {"nome": "TRADE_DATE", "inicio": 3, "tamanho": 8},
                {"nome": "TICKER", "inicio": 11, "tamanho": 12},
            ],
            "pular_linhas": 1,
            "descartar_ultimas": 1,
        },
        "schema_esperado": ["TRADE_DATE", "TICKER"],
    }
    linhas_txt = [
        "00HEADER" + " " * 20,
        "0120260814AAA1        ",
        "0120260814BBB2        ",
        "99TRAILER" + " " * 20,
    ]
    dados = "\n".join(linhas_txt).encode("cp1252")
    parquet, n, _ = await normalizar(dados, contrato)
    assert n == 2
    registros = _ler(parquet, tmp_path)
    assert [r["TICKER"] for r in registros] == ["AAA1", "BBB2"]
    assert registros[0]["TRADE_DATE"] == "20260814"


async def test_lote_sem_registros_e_erro_nomeado() -> None:
    contrato = {
        "classe": "rest_api",
        "dataset": "rates_daily",
        "janela": {"tipo": "periodo"},
        "acesso": {"url_padrao": "https://example.com"},
        "formato": {"tipo": "json"},
    }
    with pytest.raises(ErroNormalizacao) as e:
        await normalizar(b"[]", contrato)
    assert e.value.codigo == "lote_sem_registros"


async def test_prefixo_linha_recorta_secao(tmp_path) -> None:
    """Two tables glued into a single report file. Without the cut, either the parse breaks on the
    ragged line or bronze becomes two stacked tables pretending to be one."""
    contrato = {
        "classe": "http_file",
        "dataset": "index_daily",
        "janela": {"tipo": "diaria"},
        "acesso": {"url_padrao": "https://example.com/index.txt"},
        "formato": {
            "tipo": "csv",
            "encoding": "latin-1",
            "delimitador": "@",
            "prefixo_linha": "1@",
            "pular_linhas": 1,
        },
        "schema_esperado": ["INDEX"],
    }
    arquivo = "\n".join(
        [
            "0@Example Report Header",
            "1@TOTALS",
            "1@Date@INDEX@Index Value",
            "1@14/08/2026@IDX-A 1@20731,10",
            "1@14/08/2026@IDX-A 1+@24716,81",
            "2@PORTFOLIO COMPOSITION",
            "2@Date@INDEX@Title@Maturity",
            "2@14/08/2026@IDX-A 1@AAA@2027-01-01",
        ]
    )
    parquet, linhas, colunas = await normalizar(arquivo.encode("latin-1"), contrato)
    assert linhas == 2, "only the 1@ section rows, without the title or the 2@ section"
    assert colunas == ["Date", "INDEX", "Index Value"]
    assert _ler(parquet, tmp_path)[0]["INDEX"] == "IDX-A 1"


async def test_prefixo_linha_sem_nenhuma_correspondencia() -> None:
    contrato = {
        "classe": "http_file",
        "dataset": "index_daily",
        "janela": {"tipo": "diaria"},
        "acesso": {"url_padrao": "https://example.com/index.txt"},
        "formato": {
            "tipo": "csv",
            "encoding": "latin-1",
            "delimitador": "@",
            "prefixo_linha": "9@",
        },
    }
    with pytest.raises(ErroNormalizacao) as e:
        await normalizar("1@a@b\n1@c@d".encode("latin-1"), contrato)
    assert e.value.codigo == "prefixo_sem_linha"


async def test_zip_com_tabelas_diferentes_e_recusado() -> None:
    """A bundle zip with csv files of distinct grains. Stacking them "works" and produces a Parquet
    that lies about its own grain - failing here is the right thing."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("general.csv", "ID;Date;Issuer\n1;2026-08-01;X\n".encode("latin-1"))
        zf.writestr("portfolio.csv", "ID;Asset;Amount\n1;AAA;10\n".encode("latin-1"))
    contrato = _contrato_csv(schema_esperado=None)
    with pytest.raises(ErroNormalizacao) as e:
        await normalizar(buf.getvalue(), contrato)
    assert e.value.codigo == "zip_multitabela"
    assert "arquivo_no_zip" in e.value.mensagem


async def test_zip_com_mesmo_layout_continua_unindo(tmp_path) -> None:
    """The guard must not get in the way of the legitimate case: a historical series with the SAME
    layout, even if the column order changes between members."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("parte_a.csv", CSV_LATIN1.encode("latin-1"))
        cab, linha = CSV_LATIN1.splitlines()
        cols = cab.split(";")
        vals = linha.split(";")
        ordem = [3, 0, 1, 2]
        cab_b = ";".join(cols[i] for i in ordem)
        linha_b = ";".join(vals[i] for i in ordem)
        invertido = f"{cab_b}\n{linha_b}\n"
        zf.writestr("parte_b.csv", invertido.encode("latin-1"))
    parquet, linhas, _ = await normalizar(buf.getvalue(), _contrato_csv())
    assert linhas == 2


async def test_json_odata_sem_json_path_acha_a_lista(tmp_path) -> None:
    """OData connector envelope (`value` + `_paginas` + `_truncado`) without a declared `json_path`:
    without finding the list, the whole envelope became ONE row and schema_esperado failed saying
    the real column does not exist - a message that sends you to debug the origin, not the code."""
    contrato = {
        "classe": "odata",
        "dataset": "quotes_monthly",
        "janela": {"tipo": "mensal"},
        "acesso": {"url_padrao": "https://example.com"},
        "formato": {"tipo": "json", "encoding": "utf-8"},
        "schema_esperado": ["bidPrice", "askPrice"],
    }
    corpo = json.dumps(
        {
            "value": [{"bidPrice": 5.4, "askPrice": 5.5, "quotedAt": "2026-07-01"}],
            "_paginas": 1,
            "_truncado": False,
        }
    ).encode()
    parquet, linhas, colunas = await normalizar(corpo, contrato)
    assert linhas == 1
    assert colunas == ["bidPrice", "askPrice", "quotedAt"]
    assert _ler(parquet, tmp_path)[0]["bidPrice"] == "5.4"


def test_diagnostico_de_linha_torta_aponta_arquivo_e_linha() -> None:
    """Real data arrives malformed: ONE row in thousands may have `;` inside an unquoted field.
    DuckDB only says "error sniffing /tmp/parte_0.csv" - without the ORIGIN file and the line,
    finding it inside a zip is a manual hunt."""
    from cflow_core.ingestao.bronze import _diagnosticar_csv

    csv = (
        "ID;Code;Type\n"
        "001;AB123;Primary\n"
        "002;Minutes of the meeting (27052026, 04052026); Summary;Secondary\n"
    )
    fmt = {"tipo": "csv", "encoding": "latin-1", "delimitador": ";"}
    achado = _diagnosticar_csv([("entities_2026.csv", csv.encode("latin-1"))], fmt)
    assert achado is not None
    assert "entities_2026.csv" in achado
    assert "line 3" in achado
    assert "4 fields" in achado and "3" in achado


def test_diagnostico_nao_acusa_csv_integro() -> None:
    from cflow_core.ingestao.bronze import _diagnosticar_csv

    fmt = {"tipo": "csv", "encoding": "latin-1", "delimitador": ";"}
    assert _diagnosticar_csv([("ok.csv", CSV_LATIN1.encode("latin-1"))], fmt) is None


def test_odata_codifica_a_aspa_do_filtro() -> None:
    """Some OData services reject `$filter=Date ge '2026-07-01'` with HTTP 400 and accept the SAME
    query with `%27`. httpx sends the raw quote (correct per RFC, valid sub-delim) - hence
    `codificar_estrito`. Without it such a service is never ingested, and the error is an
    unexplained 400."""
    from urllib.parse import quote, urlencode

    params = {"$format": "json", "$filter": "Date ge '2026-07-01' and Date le '2026-07-31'"}
    qs = urlencode(params, quote_via=quote, safe="")
    assert "%27" in qs, "the quote must be encoded"
    assert "%20" in qs and "+" not in qs, "space as %20; such services also reject '+'"


# --- Invalid-row tolerance (per contract) ----------------------------------------------------

# The middle row has one extra `;` inside an unquoted field - a typical defect where a free-text
# field arrives with the delimiter in it.
CSV_COM_LINHA_RAGGED = (
    "ENTITY_ID;REF_DATE;PRICE;NAME\n"
    "000-001;2026-01-31;1,2345;GOOD\n"
    "000-002;2026-01-31;9,99;Minutes of the meeting (27052026); Summary\n"
    "000-003;2026-01-31;3,33;ANOTHER GOOD\n"
)


async def test_sem_tolerancia_linha_ragged_manda_o_lote_para_a_quarentena() -> None:
    """The default is still to stop: an unexpected row is almost always the origin changing its
    layout, and moving on would hide that."""
    dados = _zip("inf.csv", CSV_COM_LINHA_RAGGED.encode("latin-1"))
    with pytest.raises(ErroNormalizacao) as e:
        await normalizar(dados, _contrato_csv())
    assert e.value.codigo == "parse_falhou"


async def test_tolerancia_declarada_deixa_passar_e_REGISTRA_o_que_caiu(tmp_path) -> None:
    """`tolerar_linhas_invalidas` trades "I stop" for "I go on and tell you how many" - never for
    "I hide it". Without the manifest record, it would be `ignore_errors` under another name."""
    contrato = _contrato_csv()
    contrato["formato"]["tolerar_linhas_invalidas"] = True
    diagnostico: dict = {}
    dados = _zip("inf.csv", CSV_COM_LINHA_RAGGED.encode("latin-1"))
    parquet, linhas, _ = await normalizar(dados, contrato, diagnostico=diagnostico)

    assert linhas == 2, "the two good rows get in"
    assert {ln["ENTITY_ID"] for ln in _ler(parquet, tmp_path)} == {"000-001", "000-003"}
    assert diagnostico["linhas_rejeitadas"] == 1
    assert diagnostico["rejeicoes"][0]["linha"] == 3


async def test_sem_tolerancia_o_diagnostico_fica_vazio() -> None:
    """Whoever did not ask for tolerance does not get a new field in the manifest."""
    diagnostico: dict = {}
    await normalizar(
        _zip("inf.csv", CSV_LATIN1.encode("latin-1")), _contrato_csv(), diagnostico=diagnostico
    )
    assert diagnostico == {}


async def test_tolerancia_sem_nada_a_rejeitar_nao_inventa_registro() -> None:
    """A clean file with the flag on must not come out marked as if it had lost rows."""
    contrato = _contrato_csv()
    contrato["formato"]["tolerar_linhas_invalidas"] = True
    diagnostico: dict = {}
    await normalizar(
        _zip("inf.csv", CSV_LATIN1.encode("latin-1")), contrato, diagnostico=diagnostico
    )
    assert diagnostico == {}
