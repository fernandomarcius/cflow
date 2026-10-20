"""Silver as an Iceberg table.

What these tests pin is the reason for swapping loose Parquet for Iceberg: **typing a column cannot
force rewriting the dataset**, and the previous version must stay queryable. Many columns are still
to be typed - that, multiplied across them, is what is at stake.

They run against the test Postgres, which is where the JDBC catalog lives - the same one Trino
reads with `iceberg.catalog.type=jdbc`.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def lake_local(tmp_path):
    """Local lake backend - the air-gapped mode, which must work the same."""
    from cflow_core.config import get_settings
    from cflow_core.ingestao.armazenamento import ArmazenamentoLocal

    monkey = pytest.MonkeyPatch()
    monkey.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    try:
        yield ArmazenamentoLocal(raiz=tmp_path / "lake")
    finally:
        monkey.undo()
        get_settings.cache_clear()


@pytest.fixture
def limpar_catalogo():
    """The catalog is a table in Postgres: without cleaning, one test sees the other's table."""
    yield
    import sqlalchemy as sa

    from cflow_core.ingestao.iceberg import _url_sincrona

    # The tables come from the migration: clean the CONTENT, not the schema.
    e = sa.create_engine(_url_sincrona())
    with e.begin() as c:
        c.execute(sa.text("DELETE FROM iceberg_tables"))
        c.execute(sa.text("DELETE FROM iceberg_namespace_properties"))
    e.dispose()


def _tabela(linhas: list[dict], esquema):
    import pyarrow as pa

    return pa.Table.from_pylist(linhas, schema=esquema)


async def test_publica_e_cria_snapshot(lake_local, limpar_catalogo) -> None:
    import pyarrow as pa

    from cflow_core.ingestao import iceberg

    esq = pa.schema([("tax_id", pa.string()), ("valor", pa.string())])
    r = await iceberg.publicar(
        lake=lake_local,
        dataset="registry_general",
        tabela_arrow=_tabela([{"tax_id": "111", "valor": "1,50"}], esq),
    )
    assert r["tabela"] == "silver.registry_general"
    assert r["snapshot_id"] and r["linhas"] == 1
    assert r["metadata_location"].endswith(".metadata.json")


async def test_reexecucao_substitui_o_dado_e_PRESERVA_a_versao_anterior(
    lake_local, limpar_catalogo
) -> None:
    """Silver is recomputed whole on every run, so the operation is `overwrite` - which in Iceberg
    deletes nothing: it creates a new snapshot. It is what answers "what the report showed in June"
    without keeping duplicate Parquet."""
    import pyarrow as pa

    from cflow_core.ingestao import iceberg

    esq = pa.schema([("tax_id", pa.string())])
    await iceberg.publicar(
        lake=lake_local,
        dataset="registry_general",
        tabela_arrow=_tabela([{"tax_id": "111"}], esq),
        propriedades={"cflow.execucao_id": "exec-1"},
    )
    r2 = await iceberg.publicar(
        lake=lake_local,
        dataset="registry_general",
        tabela_arrow=_tabela([{"tax_id": "222"}, {"tax_id": "333"}], esq),
        propriedades={"cflow.execucao_id": "exec-2"},
    )

    hist = await iceberg.historico(lake_local, "registry_general")
    assert len(hist) == 2, "the previous version stays in the history"
    assert r2["linhas"] == 2

    cat = await iceberg.abrir_catalogo(lake_local)
    t = cat.load_table("silver.registry_general")
    agora = t.scan().to_arrow()
    antes = t.scan(snapshot_id=int(hist[0]["snapshot_id"])).to_arrow()
    assert agora.num_rows == 2 and antes.num_rows == 1
    assert antes.column("tax_id").to_pylist() == ["111"]


async def test_tipar_coluna_nao_reescreve_o_dataset(lake_local, limpar_catalogo) -> None:
    """The criterion that decided the choice. The column is born VARCHAR (bronze is text) and one day
    is typed by the specialist - with loose Parquet that would force recreating the external table
    and metastore."""
    import pyarrow as pa

    from cflow_core.ingestao import iceberg

    await iceberg.publicar(
        lake=lake_local,
        dataset="registry_general",
        tabela_arrow=_tabela(
            [{"tax_id": "111", "vl": "10.5"}],
            pa.schema([("tax_id", pa.string()), ("vl", pa.string())]),
        ),
        propriedades={"cflow.execucao_id": "exec-1"},
    )
    # now `vl` becomes a number and a new column comes in
    novo = pa.schema([("tax_id", pa.string()), ("vl", pa.decimal128(28, 10)), ("dt", pa.date32())])
    r = await iceberg.publicar(
        lake=lake_local,
        dataset="registry_general",
        tabela_arrow=_tabela([{"tax_id": "111", "vl": None, "dt": None}], novo),
        propriedades={"cflow.execucao_id": "exec-2"},
    )
    assert "dt" in r["colunas"]
    assert len(await iceberg.historico(lake_local, "registry_general")) == 2


async def test_metadata_sai_do_catalogo_nunca_de_glob(lake_local, limpar_catalogo) -> None:
    """It is the path Trino and DuckDB need. It comes from the catalog because only it knows which one
    is current - listing the bucket would return every metadata file of every version."""
    import pyarrow as pa

    from cflow_core.ingestao import iceberg

    await iceberg.publicar(
        lake=lake_local,
        dataset="registry_general",
        tabela_arrow=_tabela([{"a": "x"}], pa.schema([("a", pa.string())])),
    )
    caminho = await iceberg.metadata_de(lake_local, "registry_general")
    assert caminho.endswith(".metadata.json")


async def test_dataset_ausente_e_erro_nomeado(lake_local, limpar_catalogo) -> None:
    from cflow_core.errors import ErroAplicacao
    from cflow_core.ingestao import iceberg

    with pytest.raises(ErroAplicacao) as e:
        await iceberg.metadata_de(lake_local, "does_not_exist")
    assert e.value.codigo == "iceberg_tabela_ausente"


@pytest.mark.parametrize("ruim", ["with-hyphen", "Uppercase", "with space", "", "../escape"])
async def test_nome_de_dataset_invalido_recusado(lake_local, ruim: str) -> None:
    """The dataset becomes a table name in the catalog, read by query engines."""
    from cflow_core.errors import ErroAplicacao
    from cflow_core.ingestao import iceberg

    with pytest.raises(ErroAplicacao) as e:
        await iceberg.metadata_de(lake_local, ruim)
    assert e.value.codigo == "iceberg_dataset_invalido"


def test_url_do_catalogo_usa_driver_permissivo() -> None:
    """`psycopg` is LGPL and the licence check rejects it; PyIceberg needs synchronous SQLAlchemy,
    which does not accept asyncpg. `pg8000` (BSD) is the bridge - and the translation must not be
    lost."""
    from cflow_core.ingestao.iceberg import _url_sincrona

    url = _url_sincrona()
    assert "+pg8000" in url
    assert "asyncpg" not in url


def test_destino_iceberg_aceito_na_definicao() -> None:
    from cflow_core.pipelines.definicao import validar

    validar(
        {
            "nos": [
                {"id": "f", "tipo": "fonte", "config": {"origem": "lake", "fonte_id": "x"}},
                {
                    "id": "s",
                    "tipo": "saida",
                    "config": {"destino": "iceberg", "dataset": "registry_general"},
                },
            ],
            "arestas": [{"de": "f", "para": "s"}],
        }
    )


def test_destino_iceberg_recusa_hifen_no_dataset() -> None:
    """A hyphen would force quoting the identifier in every query."""
    from cflow_core.errors import ErroAplicacao
    from cflow_core.pipelines.definicao import validar

    with pytest.raises(ErroAplicacao) as e:
        validar(
            {
                "nos": [
                    {"id": "f", "tipo": "fonte", "config": {"origem": "lake", "fonte_id": "x"}},
                    {
                        "id": "s",
                        "tipo": "saida",
                        "config": {"destino": "iceberg", "dataset": "registry-general"},
                    },
                ],
                "arestas": [{"de": "f", "para": "s"}],
            }
        )
    assert "table name" in str(e.value.detalhes)


async def test_time_travel_sobrevive_a_troca_de_tipo(lake_local, limpar_catalogo) -> None:
    """The central promise of the design, and the one that could most easily have failed.

    Iceberg REFUSES `string -> decimal` as a safe promotion - `allow_incompatible_changes` was
    needed. The remaining question is whether the previous snapshot is still readable afterwards: if
    it were not, typing a column would erase its history, and the reason for choosing Iceberg would
    fall with it."""
    from decimal import Decimal

    import pyarrow as pa

    from cflow_core.ingestao import iceberg

    v1 = pa.schema([("tax_id", pa.string()), ("vl", pa.string())])
    r1 = await iceberg.publicar(
        lake=lake_local,
        dataset="registry_general",
        tabela_arrow=_tabela([{"tax_id": "1", "vl": "10.5"}], v1),
        propriedades={"cflow.execucao_id": "exec-1"},
    )
    v2 = pa.schema([("tax_id", pa.string()), ("vl", pa.decimal128(28, 10))])
    await iceberg.publicar(
        lake=lake_local,
        dataset="registry_general",
        tabela_arrow=_tabela([{"tax_id": "1", "vl": Decimal("10.5")}], v2),
        propriedades={"cflow.execucao_id": "exec-2"},
    )

    cat = await iceberg.abrir_catalogo(lake_local)
    t = cat.load_table("silver.registry_general")
    assert t.scan().to_arrow().column("vl").to_pylist() == [Decimal("10.5000000000")]
    antigo = t.scan(snapshot_id=int(r1["snapshot_id"])).to_arrow()
    assert antigo.column("vl").to_pylist() == ["10.5"], "the snapshot keeps the schema of the time"


async def test_historico_conta_publicacoes_nao_snapshots(lake_local, limpar_catalogo) -> None:
    """`overwrite` emits two snapshots (delete + append). Saying "3 versions" to someone who
    published twice would be a lie - and it is the number someone would use to pick a date."""
    import pyarrow as pa

    from cflow_core.ingestao import iceberg

    esq = pa.schema([("a", pa.string())])
    for i in range(3):
        await iceberg.publicar(
            lake=lake_local,
            dataset="registry_general",
            tabela_arrow=_tabela([{"a": str(i)}], esq),
            propriedades={"cflow.execucao_id": f"exec-{i}"},
        )
    hist = await iceberg.historico(lake_local, "registry_general")
    assert [h["execucao_id"] for h in hist] == ["exec-0", "exec-1", "exec-2"]
    assert all(h["operacao"] != "delete" for h in hist)


async def test_pipeline_executa_e_publica_no_iceberg(
    cliente, owner_engine, app_engine, tmp_path, limpar_catalogo
) -> None:
    """End to end through the ENGINE, which is the production path.

    It exists because the isolated module test did not catch the real bug: the engine used
    `.arrow()`, which returns a RecordBatchReader, and PyIceberg only accepts `pa.Table`. Testing
    the piece without testing the fit lets exactly that kind of thing through."""
    from datetime import date

    from sqlalchemy import text

    from cflow_core.config import get_settings
    from cflow_core.ingestao import iceberg

    from ._arnes_auth import usuario_logado
    from .conftest import aguardar_execucao
    from .test_lake_leitura import _semear_bronze_local, _semear_fonte

    monkey = pytest.MonkeyPatch()
    monkey.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    try:
        fonte = await _semear_fonte(app_engine, "ds_ice")
        uri = await _semear_bronze_local(
            tmp_path / "lake",
            "ds_ice",
            "2026",
            "c" * 64,
            [{"tax_id": "111", "vl": "1,50"}, {"tax_id": "222", "vl": "2,50"}],
        )
        async with app_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO ingestao_lotes (fonte_id, chave_lote, sha256, status, linhas,"
                    " bronze_uri, ingestion_date) VALUES (:f,'2026',:s,'bronze',2,:u,:d)"
                ),
                {"f": fonte, "s": "c" * 64, "u": uri, "d": date(2026, 8, 15)},
            )

        _, headers = await usuario_logado(
            owner_engine, cliente, email="ice@example.com", perfil="admin"
        )
        definicao = {
            "nos": [
                {"id": "n1", "tipo": "fonte", "config": {"origem": "lake", "fonte_id": str(fonte)}},
                {"id": "n2", "tipo": "sql", "sql": 'SELECT tax_id, vl FROM "n1" ORDER BY tax_id'},
                {
                    "id": "n3",
                    "tipo": "saida",
                    "config": {"destino": "iceberg", "dataset": "ds_ice"},
                },
            ],
            "arestas": [{"de": "n1", "para": "n2"}, {"de": "n2", "para": "n3"}],
        }
        pid = (
            await cliente.post("/pipelines", json={"nome": "silver ice"}, headers=headers)
        ).json()["id"]
        await cliente.put(
            f"/pipelines/{pid}/rascunho",
            json={"definicao": definicao, "base_numero": 0},
            headers=headers,
        )
        pub = await cliente.post(
            f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=headers
        )
        assert pub.status_code == 201, pub.text

        ex = await cliente.post(f"/pipelines/{pid}/executar", json={}, headers=headers)
        detalhe = await aguardar_execucao(cliente, ex.json()["id"], headers)
        assert detalhe["status"] == "concluida", detalhe.get("erro")

        from cflow_core.ingestao.armazenamento import ArmazenamentoLocal

        lake = ArmazenamentoLocal(raiz=tmp_path / "lake")
        cat = await iceberg.abrir_catalogo(lake)
        linhas = cat.load_table("silver.ds_ice").scan().to_arrow().to_pylist()
        assert [x["tax_id"] for x in linhas] == ["111", "222"]
        hist = await iceberg.historico(lake, "ds_ice")
        assert len(hist) == 1 and hist[0]["execucao_id"] == ex.json()["id"]
    finally:
        monkey.undo()
        get_settings.cache_clear()
