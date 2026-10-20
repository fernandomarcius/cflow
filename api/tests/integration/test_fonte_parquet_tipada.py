"""A PARQUET file source must reach the node's SQL with the TYPE the file declares.

The generic connection-backed source path converts everything to text: `conectores/base.serial`
applies `float(Decimal)` and `_carregar` declares `VARCHAR` for every column (which is RIGHT for
CSV/Excel, where there is no type and VARCHAR saves the leading zero of an identifier). For
parquet that is destructive, because the type is written in the file.

Why this is blocking and not cosmetic: another pipeline's output (`origem: "pipeline"`) and the
lake dataset already come in through `read_parquet`, typed. In a ZERO-tolerance gate comparing the
two sides, one typed side and one all-VARCHAR side make the CAST written in the comparison SQL
decide the cent: three casts of the same column, three different numbers.
"""

from __future__ import annotations

import decimal

import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao

pytestmark = pytest.mark.integration


def _gravar_parquet(caminho) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    tabela = pa.table(
        {
            # exact money: this is what the gate compares
            "AMOUNT": pa.array(
                [decimal.Decimal("1000.00"), decimal.Decimal("1234.56")],
                type=pa.decimal128(6, 2),
            ),
            # identifier with a leading zero: must not become a number
            "TAX_ID": pa.array(["00123456789", "00000000012345"], type=pa.string()),
            "DAY": pa.array([pa.scalar("2026-06-30").as_py()], type=pa.string()).take([0, 0]),
        }
    )
    pq.write_table(tabela, caminho)


async def _conexao_arquivo(cliente, h, caminho) -> str:
    r = await cliente.post(
        "/conexoes",
        json={
            "nome": f"pq-{caminho.name}",
            "tipo": "arquivo",
            "config": {"caminho": str(caminho), "formato": "parquet"},
        },
        headers=h,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def test_decimal_do_parquet_chega_como_decimal_no_no(
    owner_engine, app_engine, cliente, tmp_path
):
    caminho = tmp_path / "base.parquet"
    _gravar_parquet(caminho)
    _, h = await usuario_logado(
        owner_engine, cliente, email="pqtipo@example.com", perfil="especialista"
    )
    cx = await _conexao_arquivo(cliente, h, caminho)

    pid = (await cliente.post("/pipelines", json={"nome": "pq-tipada"}, headers=h)).json()["id"]
    definicao = {
        "nos": [
            {
                "id": "src",
                "tipo": "fonte",
                "config": {"conexao_id": cx, "sql": "SELECT * FROM src"},
            },
            {
                "id": "tipos",
                "tipo": "sql",
                # `typeof` answers what actually arrived, which is the question of the test.
                "sql": (
                    "SELECT any_value(typeof(AMOUNT)) AS t_amount, "
                    "any_value(typeof(TAX_ID)) AS t_tax_id, "
                    "CAST(SUM(AMOUNT) AS VARCHAR) AS soma, min(TAX_ID) AS cnpj FROM src"
                ),
            },
        ],
        "arestas": [{"de": "src", "para": "tipos"}],
    }
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )

    r = await cliente.post(f"/pipelines/{pid}/executar", json={}, headers=h)
    assert r.status_code == 200, r.text
    corpo = r.json()
    det = (
        corpo
        if corpo["status"] != "executando"
        else await aguardar_execucao(cliente, corpo["id"], h)
    )
    assert det["status"] == "concluida", det

    async with app_engine.connect() as conn:
        snap = (
            await conn.execute(
                text(
                    "SELECT snapshot FROM execucao_nos WHERE execucao_id = :e AND no_id = 'tipos'"
                ),
                {"e": det["id"]},
            )
        ).scalar_one()

    import duckdb

    linha = duckdb.connect().execute(f"SELECT * FROM read_parquet('{snap}')").fetchone()
    t_amount, t_tax_id, soma, cnpj = linha

    assert t_amount.startswith("DECIMAL"), (
        f"AMOUNT arrived as {t_amount}: the parquet type was destroyed along the source path"
    )
    # The sum must be EXACT. As a DOUBLE it comes out 2234.5599999999999, and a zero-tolerance
    # gate would report a divergence the data does not have.
    assert soma == "2234.56", soma
    # And what must NOT happen along with it: the identifier turning into a number and losing its
    # leading zero.
    assert t_tax_id == "VARCHAR", t_tax_id
    assert cnpj == "00000000012345", cnpj


async def test_csv_continua_varchar_para_nao_comer_zero_a_esquerda(
    owner_engine, app_engine, cliente, tmp_path
):
    """The counterpoint: CSV does NOT get type inference. With no type declared in the file,
    inferring is guessing, and guessing eats the identifier's leading zero and turns `0.00`
    into `0`."""
    caminho = tmp_path / "base.csv"
    caminho.write_text("TAX_ID,AMOUNT\n00123456789,1000.00\n", encoding="utf-8")
    _, h = await usuario_logado(
        owner_engine, cliente, email="csvtipo@example.com", perfil="especialista"
    )
    r = await cliente.post(
        "/conexoes",
        json={
            "nome": "csv-varchar",
            "tipo": "arquivo",
            "config": {"caminho": str(caminho), "formato": "csv"},
        },
        headers=h,
    )
    cx = r.json()["id"]

    pid = (await cliente.post("/pipelines", json={"nome": "csv-varchar"}, headers=h)).json()["id"]
    definicao = {
        "nos": [
            {
                "id": "src",
                "tipo": "fonte",
                "config": {"conexao_id": cx, "sql": "SELECT * FROM src"},
            },
            {
                "id": "tipos",
                "tipo": "sql",
                "sql": (
                    "SELECT any_value(typeof(TAX_ID)) AS t_tax_id, min(TAX_ID) AS cnpj FROM src"
                ),
            },
        ],
        "arestas": [{"de": "src", "para": "tipos"}],
    }
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    r = await cliente.post(f"/pipelines/{pid}/executar", json={}, headers=h)
    corpo = r.json()
    det = (
        corpo
        if corpo["status"] != "executando"
        else await aguardar_execucao(cliente, corpo["id"], h)
    )
    assert det["status"] == "concluida", det

    async with app_engine.connect() as conn:
        snap = (
            await conn.execute(
                text(
                    "SELECT snapshot FROM execucao_nos WHERE execucao_id = :e AND no_id = 'tipos'"
                ),
                {"e": det["id"]},
            )
        ).scalar_one()

    import duckdb

    t_tax_id, cnpj = duckdb.connect().execute(f"SELECT * FROM read_parquet('{snap}')").fetchone()
    assert t_tax_id == "VARCHAR", t_tax_id
    assert cnpj == "00123456789", "the identifier's leading zero was eaten"
