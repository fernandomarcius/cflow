"""The slice becomes a column: recovering the grain lost by daily sources.

Context: a daily source accumulated rows that were byte-for-byte identical. They were not
duplicates: they were the same item on different days. The competence date existed in the PATH
(`.../ingestion_date=2026-08-15/2026-02-23/<sha12>/`) but as a BARE directory, so no reader
projected it and the table could not answer "what was the rate on 03/15".

The test that matters here is `test_competencia_vem_do_indice_nao_do_caminho`: the slice key comes
from `ParteLake.chave` (the catalogue's `chave_lote`, `NOT NULL`) and goes into the SQL as a bound
literal. It used to be re-derived from the filesystem by `regexp_extract` on `filename`; the reader
was coupled to the bronze layout and `NULL` was a possible outcome.
"""

from __future__ import annotations

from datetime import date

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.ingestao import contrato as C
from cflow_core.ingestao import motor
from cflow_core.ingestao.leitura import COLUNA_COMPETENCIA, ParteLake, sql_leitura


def _parte(caminho: str, chave: str = "x") -> ParteLake:
    return ParteLake(chave=chave, caminho=caminho, uri=f"gs://b/{caminho}", sha256="s", linhas=1)


# --- the contract must DECLARE; inferring is what produced the bug -------------------------


def test_padrao_desligado_por_omissao():
    """A source that declares nothing gets no column. Projecting by mistake would label a whole
    yearly file (slice = `2026`) with a single date."""
    assert C.fatia_e_competencia({"janela": {"tipo": "ano"}}) is False
    assert C.fatia_e_competencia({}) is False


def test_declarado_ligado():
    assert C.fatia_e_competencia({"janela": {"tipo": "dia", "fatia_e_competencia": True}}) is True


def test_validacao_recusa_tipo_errado():
    base = {
        "classe": "arquivo",
        "dataset": "registry_daily",
        "janela": {"tipo": "dia", "fatia_e_competencia": "sim"},
        "formato": {"tipo": "csv"},
        "acesso": {"url": "https://x/y.csv"},
    }
    with pytest.raises(ErroAplicacao) as e:
        C.validar(base)
    assert any("fatia_e_competencia" in (d.mensagem or "") for d in (e.value.detalhes or []))


# --- the key comes from the index, not from the filesystem ---------------------------------


def test_competencia_vem_do_indice_nao_do_caminho(tmp_path):
    """The Parquet sits at an ARBITRARY path, outside the engine layout, and the competence still
    comes out right.

    This test proves the decoupling: `ParteLake.chave` is the `chave_lote` that
    `repositorio.lotes_bronze` loaded from the catalogue (`NOT NULL`), and it is what goes into
    the SQL as a bound literal. With regex extraction on the path, this same file would return
    `NULL`: the reader depended on the bronze layout to know which slice a row belongs to."""
    solto = tmp_path / "qualquer" / "lugar.parquet"
    solto.parent.mkdir(parents=True)
    pq.write_table(pa.table({"codigo": ["DEB1"]}), solto)

    sql, binds = sql_leitura([_parte(str(solto), "2026-02-23")], competencia=True)
    tabela = duckdb.connect().execute(sql, binds).to_arrow_table()

    assert tabela.column(COLUNA_COMPETENCIA).to_pylist() == ["2026-02-23"]
    # no path string comparison: the Parquet row is still there, whole
    assert tabela.column("codigo").to_pylist() == ["DEB1"]


# --- behaviour of the generated SQL ---------------------------------------------------------


def test_sql_sem_flag_nao_muda_nada():
    sql, binds = sql_leitura([_parte("/tmp/a.parquet")])
    assert "filename" not in sql
    assert COLUNA_COMPETENCIA not in sql
    assert binds == [["/tmp/a.parquet"]]


def test_sql_com_flag_vincula_a_chave_e_nao_le_o_caminho():
    sql, binds = sql_leitura(
        [_parte("/tmp/a.parquet", "2026-02-23"), _parte("/tmp/b.parquet", "2026-02-24")],
        competencia=True,
    )
    assert f"AS {COLUNA_COMPETENCIA}" in sql
    assert "UNION ALL BY NAME" in sql
    # the path is neither read nor compared: no `filename`, no regex, no NULLIF
    assert "filename" not in sql
    assert "regexp_extract" not in sql
    assert "NULLIF" not in sql
    # bind order = order of appearance in the text: key, then path, per part
    assert binds == ["2026-02-23", "/tmp/a.parquet", "2026-02-24", "/tmp/b.parquet"]


# --- end to end in DuckDB: this is where the projection proves it works ---------------------


def _grava(tmp_path, dataset: str, ingest: date, fatia: str, sha12: str, linhas: int):
    rel = motor.caminho_bronze(dataset, ingest, fatia, sha12)
    destino = tmp_path / rel
    destino.parent.mkdir(parents=True, exist_ok=True)
    tabela = pa.table({"codigo": ["DEB1"] * linhas, "taxa": ["4,15"] * linhas})
    pq.write_table(tabela, destino)
    return str(destino)


def test_duas_fatias_deixam_de_ser_indistinguiveis(tmp_path):
    """The bug and its fix reproduced in the same test.

    Two slices with IDENTICAL bytes: without the projection they become 4 indistinguishable rows;
    with it, each row knows its day."""
    dataset = "registry_daily"
    ingest = date(2026, 8, 15)
    partes = [
        _parte(_grava(tmp_path, dataset, ingest, "2026-02-23", "aaaaaaaaaaaa", 2), "2026-02-23"),
        _parte(_grava(tmp_path, dataset, ingest, "2026-02-24", "bbbbbbbbbbbb", 2), "2026-02-24"),
    ]
    con = duckdb.connect()

    sem_sql, binds = sql_leitura(partes)
    sem = con.execute(sem_sql, binds).to_arrow_table()
    assert sem.num_rows == 4
    assert COLUNA_COMPETENCIA not in sem.column_names
    # the defect in one line: 4 rows, 1 distinct combination
    distintas = con.execute(
        f"SELECT COUNT(DISTINCT (codigo, taxa)) FROM ({sem_sql})", binds
    ).fetchone()[0]
    assert distintas == 1

    com_sql, binds = sql_leitura(partes, competencia=True)
    com = con.execute(com_sql, binds).to_arrow_table()
    assert com.num_rows == 4
    assert COLUNA_COMPETENCIA in com.column_names
    assert "filename" not in com.column_names
    assert sorted(set(com.column(COLUNA_COMPETENCIA).to_pylist())) == ["2026-02-23", "2026-02-24"]
    distintas = con.execute(
        f"SELECT COUNT(DISTINCT (codigo, taxa, {COLUNA_COMPETENCIA})) FROM ({com_sql})", binds
    ).fetchone()[0]
    assert distintas == 2


# --- the column must survive all the way to the silver -------------------------------------


def test_gerador_silver_trata_competencia_como_proveniencia():
    from cflow_core.ingestao.gerador_silver import COLUNAS_PROVENIENCIA

    assert COLUNA_COMPETENCIA in COLUNAS_PROVENIENCIA


# --- which sources declare it: a MEASURED decision, not scattered at will ---------------------


def test_so_declara_quem_nao_tem_data_na_origem():
    """The flag is a grain declaration, not decoration. Only a source whose rows carry no date
    column of their own declares it:

        registry_daily    -> NO date column, identical rows on different days -> DECLARES
        registry_monthly  -> carries `reference_date`                         -> does not declare
        rates_daily       -> carries `date`                                   -> does not declare

    Turning the flag on for a source that already carries the date creates a second date column
    competing with the origin's, and the silver ends up with two truths about the same fact.
    """
    from cflow_core.ingestao.catalogo import CATALOGO

    declaram = {ds for ds, c in CATALOGO.items() if C.fatia_e_competencia(c)}
    assert declaram == {"registry_daily"}


def test_todo_chamador_de_sql_leitura_repassa_o_flag():
    """Guard against a mistake that is easy to repeat.

    Every production caller of `sql_leitura` must forward the flag. A forgotten caller would
    publish without `_competencia` even though the engine knows how to project it, with no error
    at all.

    A new caller that forgets the flag breaks here, not six months later in a silent table.
    """
    import pathlib
    import re

    raiz = pathlib.Path(__file__).resolve().parents[2]
    alvos = [
        raiz / "core/src/cflow_core/execucao/engine.py",
        raiz / "core/src/cflow_core/pipelines/preview.py",
    ]
    for f in alvos:
        texto = f.read_text()
        for chamada in re.finditer(r"sql_leitura\((?:[^()]|\([^()]*\))*\)", texto):
            assert "competencia=" in chamada.group(0), f"{f.name}: {chamada.group(0)[:80]}"


def test_todo_declarante_tem_janela_diaria():
    """Guard against a mistake `alinhar_a` almost made: `cadencia` is the EXECUTION frequency (a
    daily file source is still 'mensal'!), not the grain of the series. Only the window says the
    grain."""
    from cflow_core.ingestao.catalogo import CATALOGO

    for ds, c in CATALOGO.items():
        if C.fatia_e_competencia(c):
            assert (c.get("janela") or {}).get("tipo") == "diaria", ds
