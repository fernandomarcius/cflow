"""Lake read catalog (silver): which batch of each slice is valid.

What these tests pin is the difference between a consistent dataset and one that sums the same data
twice. Some sources are YEARLY files resubmitted every competence: the same `chave_lote`
accumulates versions, and reading all of them would duplicate the whole year.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

import pytest
from sqlalchemy import text

from cflow_core.ingestao import repositorio as repo

pytestmark = pytest.mark.integration


async def _semear_fonte(app_engine, dataset: str = "ds_teste") -> uuid.UUID:
    async with app_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO ingestao_fontes (nome, dataset, classe, contrato) "
                    "VALUES (:n, :d, 'http_file', CAST(:c AS jsonb)) RETURNING id"
                ),
                {"n": f"Source {dataset}", "d": dataset, "c": "{}"},
            )
        ).scalar_one()


async def _semear_lote(
    app_engine,
    fonte_id,
    chave: str,
    sha: str,
    *,
    status: str = "bronze",
    linhas: int = 10,
    momento: datetime | None = None,
) -> None:
    async with app_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO ingestao_lotes (fonte_id, chave_lote, sha256, status, linhas, "
                "bronze_uri, ingestion_date, criado_em) VALUES (:f, :c, :s, :st, :l, :u, :d, :m)"
            ),
            {
                "f": fonte_id,
                "c": chave,
                "s": sha,
                "st": status,
                "l": linhas,
                "u": (
                    f"gs://bronze/{chave}/{sha[:12]}/dados.parquet" if status == "bronze" else None
                ),
                "d": date(2026, 8, 15),
                "m": momento or datetime(2026, 8, 15, 10, 0),
            },
        )


async def test_fatia_republicada_devolve_so_o_lote_mais_novo(app_engine) -> None:
    """The yearly-file case: the year file is resubmitted every month and the slice gets a new
    version. Reading both would sum the whole year twice."""
    fonte = await _semear_fonte(app_engine)
    jul, ago = datetime(2026, 7, 10, 10, 0), datetime(2026, 8, 10, 10, 0)
    await _semear_lote(app_engine, fonte, "2026", "aaa", linhas=100, momento=jul)
    await _semear_lote(app_engine, fonte, "2026", "bbb", linhas=140, momento=ago)

    lotes = await repo.lotes_bronze(fonte)
    assert len(lotes) == 1
    assert lotes[0]["sha256"] == "bbb" and lotes[0]["linhas"] == 140


async def test_recarga_em_quarentena_nao_esconde_o_ultimo_lote_bom(app_engine) -> None:
    """If the most recent reload failed, the slice cannot vanish: the previous good data stays
    valid. That is why `status='bronze'` filters BEFORE the DISTINCT ON."""
    fonte = await _semear_fonte(app_engine)
    jul, ago = datetime(2026, 7, 10, 10, 0), datetime(2026, 8, 10, 10, 0)
    await _semear_lote(app_engine, fonte, "2026", "bom", linhas=100, momento=jul)
    await _semear_lote(app_engine, fonte, "2026", "ruim", status="quarentena", momento=ago)

    lotes = await repo.lotes_bronze(fonte)
    assert len(lotes) == 1 and lotes[0]["sha256"] == "bom"


async def test_corte_por_chave_resolve_sobreposicao_entre_datasets(app_engine) -> None:
    """A yearly dataset (2020..2026) overlaps a daily one (2026). The lexicographic cut is what lets
    the specialist use the yearly one up to 2025 and the daily one from 2026 on."""
    fonte = await _semear_fonte(app_engine, "yearly_test")
    for ano in ("2020", "2021", "2025", "2026"):
        await _semear_lote(app_engine, fonte, ano, f"sha{ano}")

    ate_2025 = await repo.lotes_bronze(fonte, chave_max="2025")
    assert [x["chave_lote"] for x in ate_2025] == ["2020", "2021", "2025"]

    de_2021 = await repo.lotes_bronze(fonte, chave_min="2021", chave_max="2025")
    assert [x["chave_lote"] for x in de_2021] == ["2021", "2025"]


async def test_ordem_lexicografica_e_cronologica_em_chave_de_periodo(app_engine) -> None:
    """A period key (`a..b`) also sorts correctly as text - which is what allows the cut."""
    fonte = await _semear_fonte(app_engine, "series_test")
    await _semear_lote(app_engine, fonte, "2010-01-01..2019-12-29", "a")
    await _semear_lote(app_engine, fonte, "2019-12-30..2029-12-26", "b")

    lotes = await repo.lotes_bronze(fonte)
    assert [x["chave_lote"] for x in lotes] == [
        "2010-01-01..2019-12-29",
        "2019-12-30..2029-12-26",
    ]


async def test_fonte_sem_lote_devolve_vazio(app_engine) -> None:
    assert await repo.lotes_bronze(await _semear_fonte(app_engine)) == []


async def test_lote_sem_bronze_uri_fica_de_fora(app_engine) -> None:
    """A batch that landed but did not reach bronze is not readable - without Parquet there is
    nothing to open."""
    fonte = await _semear_fonte(app_engine)
    await _semear_lote(app_engine, fonte, "2026", "x", status="landing")
    assert await repo.lotes_bronze(fonte) == []


# --- `lake` origin in the pipeline ------------------------------------------------------


async def _semear_bronze_local(lake_dir, dataset: str, chave: str, sha: str, linhas: list[dict]):
    """Write a Parquet to the local lake, in the SAME layout the ingestion engine uses."""
    import duckdb

    caminho = lake_dir / "bronze" / dataset / "ingestion_date=2026-08-15" / chave / sha[:12]
    caminho.mkdir(parents=True, exist_ok=True)
    alvo = caminho / "dados.parquet"
    con = duckdb.connect()
    try:
        colunas = ", ".join(f"'{v}' AS \"{k}\"" for k, v in linhas[0].items())
        uniao = " UNION ALL ".join(
            "SELECT " + ", ".join(f"'{v}' AS \"{k}\"" for k, v in ln.items()) for ln in linhas
        )
        con.execute(f"COPY ({uniao or colunas}) TO '{alvo}' (FORMAT parquet)")
    finally:
        con.close()
    return f"file://{alvo}"


async def test_pipeline_le_o_bronze_e_une_as_fatias(cliente, owner_engine, app_engine, tmp_path):
    """End to end: lake source -> specialist SQL -> snapshot. Two slices become ONE table."""
    from cflow_core.config import get_settings

    from ._arnes_auth import usuario_logado

    monkey = pytest.MonkeyPatch()
    monkey.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    try:
        fonte = await _semear_fonte(app_engine, "ds_lake_teste")
        u1 = await _semear_bronze_local(
            tmp_path / "lake",
            "ds_lake_teste",
            "2025",
            "a" * 64,
            [{"tax_id": "111", "valor": "1,50"}],
        )
        u2 = await _semear_bronze_local(
            tmp_path / "lake",
            "ds_lake_teste",
            "2026",
            "b" * 64,
            [{"tax_id": "222", "valor": "2,50"}, {"tax_id": "333", "valor": "3,50"}],
        )
        async with app_engine.begin() as conn:
            for chave, sha, uri, n in (("2025", "a" * 64, u1, 1), ("2026", "b" * 64, u2, 2)):
                await conn.execute(
                    text(
                        "INSERT INTO ingestao_lotes (fonte_id, chave_lote, sha256, status, linhas,"
                        " bronze_uri, ingestion_date) VALUES (:f,:c,:s,'bronze',:l,:u,:d)"
                    ),
                    {"f": fonte, "c": chave, "s": sha, "l": n, "u": uri, "d": date(2026, 8, 15)},
                )

        _, headers = await usuario_logado(
            owner_engine, cliente, email="lake@example.com", perfil="admin"
        )
        definicao = {
            "nos": [
                {"id": "n1", "tipo": "fonte", "config": {"origem": "lake", "fonte_id": str(fonte)}},
                {"id": "n2", "tipo": "sql", "sql": 'SELECT tax_id FROM "n1" ORDER BY tax_id'},
            ],
            "arestas": [{"de": "n1", "para": "n2"}],
        }
        r = await cliente.post("/pipelines", json={"nome": "Silver test"}, headers=headers)
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        await cliente.put(
            f"/pipelines/{pid}/rascunho",
            json={"definicao": definicao, "base_numero": 0},
            headers=headers,
        )
        pub = await cliente.post(
            f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=headers
        )
        assert pub.status_code == 201, pub.text

        from .conftest import aguardar_execucao

        ex = await cliente.post(f"/pipelines/{pid}/executar", json={}, headers=headers)
        assert ex.status_code == 200, ex.text
        detalhe = await aguardar_execucao(cliente, ex.json()["id"], headers)
        assert detalhe["status"] == "concluida", detalhe.get("erro")
        nos = {n["no_id"]: n for n in detalhe["nos"]}
        assert nos["n1"]["linhas"] == 3, "the two slices become a single table"
        assert nos["n2"]["linhas"] == 3
    finally:
        monkey.undo()
        get_settings.cache_clear()


async def test_dois_datasets_do_mesmo_zip_nao_se_confundem_no_cache(app_engine, tmp_path) -> None:
    """Several tables of different grains come out of ONE ZIP - and share a single `sha256`, because
    the sha belongs to the DOWNLOADED payload, not to the written Parquet.

    Here the backend is the local one, which returns the file itself without going through the
    cache: this test proves that RESOLUTION (index -> path -> Parquet) keeps the datasets apart. The
    cache - where the defect really was - is proven by
    `core/tests/test_ingestao_armazenamento.py::test_cache_separa_lotes_que_compartilham_o_sha`."""
    import duckdb

    from cflow_core.config import get_settings
    from cflow_core.ingestao import leitura

    monkey = pytest.MonkeyPatch()
    monkey.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    monkey.setenv("CFLOW_LAKE_CACHE_DIR", str(tmp_path / "cache"))
    get_settings.cache_clear()
    try:
        sha = "z" * 64  # the SAME for both: it is the sha of the origin ZIP
        f1 = await _semear_fonte(app_engine, "registry_general")
        f2 = await _semear_fonte(app_engine, "registry_balance")
        u1 = await _semear_bronze_local(
            tmp_path / "lake", "registry_general", "2026", sha, [{"issuer_tax_id": "111"}]
        )
        u2 = await _semear_bronze_local(
            tmp_path / "lake", "registry_balance", "2026", sha, [{"ativo_circulante": "9"}]
        )
        async with app_engine.begin() as conn:
            for fonte, uri in ((f1, u1), (f2, u2)):
                await conn.execute(
                    text(
                        "INSERT INTO ingestao_lotes (fonte_id, chave_lote, sha256, status, linhas,"
                        " bronze_uri, ingestion_date) VALUES (:f,'2026',:s,'bronze',1,:u,:d)"
                    ),
                    {"f": fonte, "s": sha, "u": uri, "d": date(2026, 8, 15)},
                )

        colunas = []
        for fonte in (f1, f2):
            partes, _ = await leitura.partes({"fonte_id": str(fonte)})
            sql, binds = leitura.sql_leitura(partes)
            con = duckdb.connect()
            try:
                con.execute(f"CREATE TABLE t AS {sql}", binds)
                colunas.append(
                    [
                        d[0]
                        for d in con.execute("SELECT * FROM t LIMIT 0").description
                        if d[0] != "ingestion_date"  # hive provenance, not part of the table
                    ]
                )
            finally:
                con.close()
        assert colunas == [["issuer_tax_id"], ["ativo_circulante"]]
    finally:
        monkey.undo()
        get_settings.cache_clear()


async def test_lake_sem_lote_falha_com_erro_nomeado(app_engine) -> None:
    """A silent empty table is the worst outcome: without a batch you cannot even know the columns."""
    from cflow_core.errors import ErroAplicacao
    from cflow_core.ingestao import leitura

    fonte = await _semear_fonte(app_engine, "ds_vazio")
    with pytest.raises(ErroAplicacao) as e:
        await leitura.resolver({"fonte_id": str(fonte)})
    assert e.value.codigo == "lake_sem_lotes"
    assert "ds_vazio" in e.value.mensagem


async def test_lake_fonte_inexistente_e_404(app_engine) -> None:
    from cflow_core.errors import ErroAplicacao
    from cflow_core.ingestao import leitura

    with pytest.raises(ErroAplicacao) as e:
        await leitura.resolver({"fonte_id": str(uuid.uuid4())})
    assert e.value.codigo == "lake_fonte_desconhecida"


# --- Publishing to silver ------------------------------------------------------------


async def _pipeline_bronze_para_silver(
    cliente, headers, fonte_id, dataset_silver: str, *, min_linhas: int = 1
) -> str:
    definicao = {
        "nos": [
            {"id": "n1", "tipo": "fonte", "config": {"origem": "lake", "fonte_id": str(fonte_id)}},
            {
                "id": "s1",
                "tipo": "saida",
                "config": {"destino": "lake", "dataset": dataset_silver, "min_linhas": min_linhas},
            },
        ],
        "arestas": [{"de": "n1", "para": "s1"}],
    }
    pid = (
        await cliente.post("/pipelines", json={"nome": f"silver {dataset_silver}"}, headers=headers)
    ).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho",
        json={"definicao": definicao, "base_numero": 0},
        headers=headers,
    )
    r = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=headers
    )
    assert r.status_code == 201, r.text
    return pid


async def test_silver_versiona_o_dado_e_move_o_ponteiro(
    cliente, owner_engine, app_engine, tmp_path
) -> None:
    """Two runs: the Parquet of each survives (immutable path) and `_atual.json` points to the newest.
    This design is what lets silver be 'rewritten' without overwriting any data - and makes a crash
    before the commit leave the previous version in force."""
    import json as _json

    from cflow_core.config import get_settings
    from cflow_core.ingestao import armazenamento

    from ._arnes_auth import usuario_logado
    from .conftest import aguardar_execucao

    monkey = pytest.MonkeyPatch()
    monkey.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    try:
        fonte = await _semear_fonte(app_engine, "ds_silver_origem")
        uri = await _semear_bronze_local(
            tmp_path / "lake",
            "ds_silver_origem",
            "2026",
            "c" * 64,
            [{"tax_id": "111", "valor": "1,50"}, {"tax_id": "222", "valor": "2,50"}],
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
            owner_engine, cliente, email="silver@example.com", perfil="admin"
        )
        pid = await _pipeline_bronze_para_silver(cliente, headers, fonte, "registry_general")

        r1 = await cliente.post(f"/pipelines/{pid}/executar", json={}, headers=headers)
        primeira = r1.json()
        d1 = await aguardar_execucao(cliente, primeira["id"], headers)
        assert d1["status"] == "concluida", d1.get("erro")

        lake = await armazenamento.abrir(None)
        p1 = _json.loads(await lake.ler("silver", "registry_general/_atual.json"))
        assert p1["linhas"] == 2 and p1["execucao_id"] == primeira["id"]
        # `ingestion_date` comes for free from the hive partitioning of the bronze path. It is useful
        # provenance and is PART of the silver contract - the external consumer will see it.
        assert p1["colunas"] == ["tax_id", "valor", "ingestion_date"]

        r2 = await cliente.post(f"/pipelines/{pid}/executar", json={}, headers=headers)
        segunda = r2.json()
        d2 = await aguardar_execucao(cliente, segunda["id"], headers)
        assert d2["status"] == "concluida", d2.get("erro")

        p2 = _json.loads(await lake.ler("silver", "registry_general/_atual.json"))
        assert p2["execucao_id"] == segunda["id"], "the pointer moves"
        # The previous version is STILL there: nothing was overwritten.
        anterior = (
            tmp_path / "lake" / "silver" / "registry_general" / f"execucao_id={primeira['id']}"
        )
        assert (anterior / "dados.parquet").is_file()
        assert (anterior / "_manifesto.json").is_file()
    finally:
        monkey.undo()
        get_settings.cache_clear()


async def test_saida_vazia_nao_move_o_ponteiro(cliente, owner_engine, app_engine, tmp_path) -> None:
    """The empty-output guard applies to silver: a result below the minimum cannot replace a good
    dataset - and, here, it does not even write a new object."""
    from cflow_core.config import get_settings

    from ._arnes_auth import usuario_logado
    from .conftest import aguardar_execucao

    monkey = pytest.MonkeyPatch()
    monkey.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    try:
        fonte = await _semear_fonte(app_engine, "ds_silver_vazio")
        uri = await _semear_bronze_local(
            tmp_path / "lake", "ds_silver_vazio", "2026", "d" * 64, [{"tax_id": "111"}]
        )
        async with app_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO ingestao_lotes (fonte_id, chave_lote, sha256, status, linhas,"
                    " bronze_uri, ingestion_date) VALUES (:f,'2026',:s,'bronze',1,:u,:d)"
                ),
                {"f": fonte, "s": "d" * 64, "u": uri, "d": date(2026, 8, 15)},
            )
        _, headers = await usuario_logado(
            owner_engine, cliente, email="silvervazio@example.com", perfil="admin"
        )
        pid = await _pipeline_bronze_para_silver(
            cliente, headers, fonte, "ds_exigente", min_linhas=99
        )
        ex = (await cliente.post(f"/pipelines/{pid}/executar", json={}, headers=headers)).json()
        detalhe = await aguardar_execucao(cliente, ex["id"], headers)

        assert detalhe["status"] == "falha"
        assert not (tmp_path / "lake" / "silver" / "ds_exigente").exists()
    finally:
        monkey.undo()
        get_settings.cache_clear()


# --- Lake preview ------------------------------------------------------------------


async def test_preview_le_recorte_e_declara_o_que_leu(
    cliente, owner_engine, app_engine, tmp_path
) -> None:
    """The preview SQL is the SAME as the run; only the data changes. But the preview reads one
    slice, not all 32 - and it must SAY so, otherwise the footer count looks like it is for the whole
    dataset and the person decides the SQL with a wrong number."""
    from cflow_core.config import get_settings

    from ._arnes_auth import usuario_logado

    monkey = pytest.MonkeyPatch()
    monkey.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    try:
        fonte = await _semear_fonte(app_engine, "ds_preview")
        uris = []
        for chave, sha, linhas in (
            ("2024", "e" * 64, [{"c": "oldest"}]),
            ("2025", "f" * 64, [{"c": "middle"}]),
            ("2026", "0" * 64, [{"c": "new1"}, {"c": "new2"}]),
        ):
            uris.append(
                await _semear_bronze_local(tmp_path / "lake", "ds_preview", chave, sha, linhas)
            )
        async with app_engine.begin() as conn:
            for (chave, sha, n), uri in zip(
                (("2024", "e" * 64, 1), ("2025", "f" * 64, 1), ("2026", "0" * 64, 2)),
                uris,
                strict=True,
            ):
                await conn.execute(
                    text(
                        "INSERT INTO ingestao_lotes (fonte_id, chave_lote, sha256, status, linhas,"
                        " bronze_uri, ingestion_date) VALUES (:f,:c,:s,'bronze',:l,:u,:d)"
                    ),
                    {"f": fonte, "c": chave, "s": sha, "l": n, "u": uri, "d": date(2026, 8, 15)},
                )

        _, headers = await usuario_logado(
            owner_engine, cliente, email="prev@example.com", perfil="admin"
        )
        pid = (
            await cliente.post("/pipelines", json={"nome": "prev lake"}, headers=headers)
        ).json()["id"]
        definicao = {
            "nos": [
                {"id": "n1", "tipo": "fonte", "config": {"origem": "lake", "fonte_id": str(fonte)}}
            ],
            "arestas": [],
        }
        r = await cliente.post(
            f"/pipelines/{pid}/nos/n1/preview", json={"definicao": definicao}, headers=headers
        )
        assert r.status_code == 200, r.text
        corpo = r.json()
        # It read only the MOST RECENT slice (2026, 2 rows) - not all three.
        assert corpo["contagem"] == 2
        assert corpo["amostra_alvo"]["fatias_lidas"] == 1
        assert corpo["amostra_alvo"]["fatias_totais"] == 3
        assert corpo["amostra_alvo"]["chaves"] == ["2026"]
    finally:
        monkey.undo()
        get_settings.cache_clear()


async def test_lote_novo_invalida_o_cache_do_preview_sem_mudar_a_definicao(
    cliente, owner_engine, app_engine, tmp_path
) -> None:
    """The case the old `_hash_fonte` allowlist missed: the definition did not change, but the data
    did. Without the batch signature in the key, the specialist keeps seeing stale data."""
    from cflow_core.config import get_settings

    from ._arnes_auth import usuario_logado

    monkey = pytest.MonkeyPatch()
    monkey.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    try:
        fonte = await _semear_fonte(app_engine, "ds_cache")
        u1 = await _semear_bronze_local(
            tmp_path / "lake", "ds_cache", "2026", "1" * 64, [{"c": "v1"}]
        )
        async with app_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO ingestao_lotes (fonte_id, chave_lote, sha256, status, linhas,"
                    " bronze_uri, ingestion_date, criado_em) VALUES "
                    "(:f,'2026',:s,'bronze',1,:u,:d,:m)"
                ),
                # Explicit `criado_em`: which batch of the slice is valid is decided by `criado_em
                # DESC`, and leaving the first on the default (`now()`) made the test depend on the
                # time of day - after 10h it became the NEWEST and beat the "republished" batch.
                {
                    "f": fonte,
                    "s": "1" * 64,
                    "u": u1,
                    "d": date(2026, 8, 15),
                    "m": datetime(2026, 8, 15, 10, 0),
                },
            )
        _, headers = await usuario_logado(
            owner_engine, cliente, email="cache@example.com", perfil="admin"
        )
        pid = (
            await cliente.post("/pipelines", json={"nome": "cache lake"}, headers=headers)
        ).json()["id"]
        definicao = {
            "nos": [
                {"id": "n1", "tipo": "fonte", "config": {"origem": "lake", "fonte_id": str(fonte)}}
            ],
            "arestas": [],
        }
        rota = f"/pipelines/{pid}/nos/n1/preview"
        antes = (await cliente.post(rota, json={"definicao": definicao}, headers=headers)).json()
        assert antes["linhas"][0]["c"] == "v1"

        # A new version of the SAME slice (the origin republished). Definition untouched.
        u2 = await _semear_bronze_local(
            tmp_path / "lake", "ds_cache", "2026", "2" * 64, [{"c": "v2"}, {"c": "v2b"}]
        )
        async with app_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO ingestao_lotes (fonte_id, chave_lote, sha256, status, linhas,"
                    " bronze_uri, ingestion_date, criado_em) VALUES "
                    "(:f,'2026',:s,'bronze',2,:u,:d,:m)"
                ),
                {
                    "f": fonte,
                    "s": "2" * 64,
                    "u": u2,
                    "d": date(2026, 8, 15),
                    "m": datetime(2026, 8, 16, 10, 0),
                },
            )

        depois = (await cliente.post(rota, json={"definicao": definicao}, headers=headers)).json()
        assert depois["contagem"] == 2, "the preview must see the new batch"
        assert {ln["c"] for ln in depois["linhas"]} == {"v2", "v2b"}
    finally:
        monkey.undo()
        get_settings.cache_clear()
