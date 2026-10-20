"""Output node (publishing) - exports the result to a stable destination + empty guard.

Covers: atomic parquet publishing (external consumption), the guard that aborts an empty
publication (preserving the previous one) and structural validation (requires a slug config.nome).
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import duckdb
import pytest

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao, contrato_ambiente_gold, parametros_ambiente_gold


async def _rodar(cliente, h, definicao: dict) -> dict:
    definicao = contrato_ambiente_gold(definicao)
    pid = (await cliente.post("/pipelines", json={"nome": "Output"}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    ex = (
        await cliente.post(
            f"/pipelines/{pid}/executar",
            json={"parametros": parametros_ambiente_gold(definicao)},
            headers=h,
        )
    ).json()
    return await aguardar_execucao(cliente, ex["id"], h)


async def test_publica_dataset(owner_engine, cliente, tmp_path):
    _, h = await usuario_logado(
        owner_engine, cliente, email="sai1@example.com", perfil="especialista"
    )
    destino = str(tmp_path / "export")
    definicao = {
        "nos": [
            {"id": "src", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1,'a'),(2,'b')) t(n,c)"},
            {"id": "out", "tipo": "saida", "config": {"nome": "ds_test", "caminho": destino}},
        ],
        "arestas": [{"de": "src", "para": "out"}],
    }
    det = await _rodar(cliente, h, definicao)
    assert det["status"] == "concluida"
    arquivo = os.path.join(destino, "ds_test.parquet")
    assert os.path.isfile(arquivo)  # published at a stable, named path
    n = duckdb.connect().execute("SELECT count(*) FROM read_parquet(?)", [arquivo]).fetchone()[0]
    assert n == 2


async def test_guard_aborta_publicacao_vazia(owner_engine, cliente, tmp_path):
    _, h = await usuario_logado(
        owner_engine, cliente, email="sai2@example.com", perfil="especialista"
    )
    destino = str(tmp_path / "export")
    definicao = {
        "nos": [
            {"id": "src", "tipo": "sql", "sql": "SELECT 1 AS n WHERE 1=0"},  # empty
            {"id": "out", "tipo": "saida", "config": {"nome": "ds_empty", "caminho": destino}},
        ],
        "arestas": [{"de": "src", "para": "out"}],
    }
    det = await _rodar(cliente, h, definicao)
    assert det["status"] in ("falha", "erro")  # the guard blocks -> the node fails
    assert not os.path.isfile(os.path.join(destino, "ds_empty.parquet"))  # nothing published


async def test_validacao_exige_nome(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="sai3@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "Invalid output"}, headers=h)).json()[
        "id"
    ]
    definicao = {
        "nos": [
            {"id": "src", "tipo": "sql", "sql": "SELECT 1 AS n"},
            {"id": "out", "tipo": "saida", "config": {}},  # without a name
        ],
        "arestas": [{"de": "src", "para": "out"}],
    }
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 422
    assert r.json()["erro"]["codigo"] == "grafo_invalido"


async def test_gold_publica_em_camada_propria(owner_engine, cliente, tmp_path, monkeypatch):
    """`destino: lake` + `camada: gold` publishes to the gold bucket, not the silver one.

    A conformed product (several tables joined and recalculated) is not the typed bronze table that
    silver promises. Without the `camada`, it would go to silver and whoever consumes silver would
    find a dataset that follows none of its rules.
    """
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()  # settings are cached: without this the test lake has no effect
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="sai4@example.com", perfil="especialista"
        )
        definicao = {
            "nos": [
                {"id": "src", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1),(2)) t(n)"},
                {
                    "id": "out",
                    "tipo": "saida",
                    "config": {"destino": "lake", "camada": "gold", "dataset": "ds_test_gold"},
                },
            ],
            "arestas": [{"de": "src", "para": "out"}],
        }
        det = await _rodar(cliente, h, definicao)
        assert det["status"] == "concluida"
        gold = tmp_path / "lake" / "gold" / "dev" / "ds_test_gold"
        assert (gold / "_atual.json").is_file()  # the external consumer pointer, in GOLD
        assert not (tmp_path / "lake" / "silver" / "ds_test_gold").exists()
        ponteiro = json.loads((gold / "_atual.json").read_text())
        # The layer goes into the manifest: whoever reads a loose object in the bucket has no other
        # way to know which contract it follows.
        assert ponteiro["camada"] == "gold"
        assert ponteiro["linhas"] == 2
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "parametros",
    [{}, {"ambiente": "qa"}],
    ids=["ausente", "invalido"],
)
async def test_disparo_gold_recusa_ambiente_ausente_ou_invalido(owner_engine, cliente, parametros):
    _, h = await usuario_logado(
        owner_engine, cliente, email="sai4b@example.com", perfil="especialista"
    )
    pid = (
        await cliente.post("/pipelines", json={"nome": "Gold requires environment"}, headers=h)
    ).json()["id"]
    definicao = contrato_ambiente_gold(
        {
            "nos": [
                {"id": "src", "tipo": "sql", "sql": "SELECT 1 AS n"},
                {
                    "id": "out",
                    "tipo": "saida",
                    "config": {
                        "destino": "lake",
                        "camada": "gold",
                        "dataset": "environment_required_ds",
                    },
                },
            ],
            "arestas": [{"de": "src", "para": "out"}],
        }
    )
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 200, r.text
    r = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    assert r.status_code == 201, r.text

    r = await cliente.post(f"/pipelines/{pid}/executar", json={"parametros": parametros}, headers=h)

    assert r.status_code == 400, r.text
    assert r.json()["erro"]["codigo"] == "parametros_invalidos"


async def test_camada_so_vale_no_destino_lake(owner_engine, cliente):
    """`camada` on a destination that is not `lake` is REFUSED, not ignored - silently accepting it
    would make the node look like it publishes to gold while publishing elsewhere."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="sai5@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "Unwanted layer"}, headers=h)).json()[
        "id"
    ]
    definicao = {
        "nos": [
            {"id": "src", "tipo": "sql", "sql": "SELECT 1 AS n"},
            {"id": "out", "tipo": "saida", "config": {"nome": "ds_x", "camada": "gold"}},
        ],
        "arestas": [{"de": "src", "para": "out"}],
    }
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 422
    assert r.json()["erro"]["codigo"] == "grafo_invalido"


async def test_camada_desconhecida_recusada(owner_engine, cliente):
    """Only `silver` and `gold`: an output node pointing at `bronze` would erase the origin evidence
    that the immutable layer exists to keep."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="sai6@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "Bronze layer"}, headers=h)).json()["id"]
    definicao = {
        "nos": [
            {"id": "src", "tipo": "sql", "sql": "SELECT 1 AS n"},
            {
                "id": "out",
                "tipo": "saida",
                "config": {"destino": "lake", "camada": "bronze", "dataset": "x"},
            },
        ],
        "arestas": [{"de": "src", "para": "out"}],
    }
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------------------------
# Partitioning of the `lake` destination
#
# Without a partition, the published path is `<dataset>/execucao_id=<uuid>/dados.parquet` and the
# only pointer is `<dataset>/_atual.json`: there is no way to ask for "the dataset of such a day,
# such a unit, such a project" - only "the last one" exists, and the second run of the same day
# steals the pointer from the first. That is what keeps a reconciliation check from being
# repeatable on a past date.
# --------------------------------------------------------------------------------------------


def _lake(tmp_path):
    """Point the test lake at `tmp` (the test run leaves no garbage in the repo) and clear the
    settings cache - without it the test `CFLOW_LAKE_DIR` has no effect."""
    _limpar_settings()
    return tmp_path / "lake"


def _limpar_settings() -> None:
    from cflow_core.config import get_settings

    get_settings.cache_clear()


async def _rodar_com(cliente, h, definicao: dict, parametros: dict, nome: str = "Output") -> dict:
    definicao = contrato_ambiente_gold(definicao)
    pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    ex = (
        await cliente.post(
            f"/pipelines/{pid}/executar",
            json={"parametros": parametros_ambiente_gold(definicao, parametros)},
            headers=h,
        )
    ).json()
    return await aguardar_execucao(cliente, ex["id"], h)


def _def_particionada(particao: list[dict], sql: str, dataset: str) -> dict:
    return {
        "parametros": [
            {"nome": "data_registro", "tipo": "as_of", "default": "hoje"},
            {"nome": "operacao_id", "tipo": "texto", "obrigatorio": True},
        ],
        "nos": [
            {"id": "src", "tipo": "sql", "sql": sql},
            {
                "id": "out",
                "tipo": "saida",
                "config": {
                    "destino": "lake",
                    "camada": "gold",
                    "dataset": dataset,
                    "particao": particao,
                },
            },
        ],
        "arestas": [{"de": "src", "para": "out"}],
    }


async def test_particao_por_parametro_entra_no_caminho(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """`data_registro` and `operacao_id` are run PARAMETERS: they become hive segments of the path and
    a pointer per scope. It is what lets you ask for the snapshot of a day without listing the
    bucket."""
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(_lake(tmp_path)))
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="par1@example.com", perfil="especialista"
        )
        det = await _rodar_com(
            cliente,
            h,
            _def_particionada(
                [
                    {"origem": "parametro", "nome": "data_registro"},
                    {"origem": "parametro", "nome": "operacao_id"},
                ],
                "SELECT * FROM (VALUES (1),(2)) t(n)",
                "ds_part_param",
            ),
            {"data_registro": "2026-08-20", "operacao_id": "61"},
        )
        assert det["status"] == "concluida"
        escopo = (
            tmp_path
            / "lake"
            / "gold"
            / "dev"
            / "ds_part_param"
            / "data_registro=2026-08-20"
            / "operacao_id=61"
        )
        assert escopo.is_dir(), "the scope (day + unit) must be a path, not loose metadata"
        assert (escopo / "_atual.json").is_file(), "pointer PER SCOPE - what is read for the day"
        ponteiro = json.loads((escopo / "_atual.json").read_text())
        assert ponteiro["linhas"] == 2
        assert ponteiro["particao"] == {"data_registro": "2026-08-20", "operacao_id": "61"}
        dados = escopo / f"execucao_id={ponteiro['execucao_id']}" / "dados.parquet"
        assert dados.is_file()
    finally:
        _limpar_settings()


async def test_particao_por_coluna_faz_fanout_mantendo_a_coluna(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """Project is not a parameter: it is a result COLUMN. One run becomes N objects, one per project -
    and the column STAYS inside the parquet (whoever reads the whole partition cannot lose the
    field)."""
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(_lake(tmp_path)))
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="par2@example.com", perfil="especialista"
        )
        det = await _rodar_com(
            cliente,
            h,
            _def_particionada(
                [
                    {"origem": "parametro", "nome": "data_registro"},
                    {"origem": "coluna", "nome": "PROJECTID", "chave": "project_id"},
                ],
                "SELECT * FROM (VALUES (1201,10),(1201,20),(1202,30)) t(PROJECTID, valor)",
                "ds_part_project",
            ),
            {"data_registro": "2026-08-20", "operacao_id": "61"},
        )
        assert det["status"] == "concluida"
        base = tmp_path / "lake" / "gold" / "dev" / "ds_part_project" / "data_registro=2026-08-20"
        assert (base / "project_id=1201" / "_atual.json").is_file()
        assert (base / "project_id=1202" / "_atual.json").is_file()
        p1 = json.loads((base / "project_id=1201" / "_atual.json").read_text())
        assert p1["linhas"] == 2  # project 1201 has 2 rows; 1202 has 1
        assert json.loads((base / "project_id=1202" / "_atual.json").read_text())["linhas"] == 1
        arq = base / "project_id=1201" / f"execucao_id={p1['execucao_id']}" / "dados.parquet"
        cols = [
            d[0]
            for d in duckdb.connect()
            .execute(f"SELECT * FROM read_parquet('{arq}') LIMIT 0")
            .description
        ]
        assert "PROJECTID" in cols, "partitioning cannot erase the column inside the file"
    finally:
        _limpar_settings()


async def test_indice_do_dataset_lista_as_particoes_da_corrida(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """With fan-out, `<dataset>/_atual.json` cannot remain "the last parquet": which of the 190
    projects would it be? It becomes the run INDEX - which scopes it produced and where. Without it,
    finding what exists would require listing the bucket, and `Armazenamento` has no (and should not
    have a) `listar()`."""
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(_lake(tmp_path)))
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="par3@example.com", perfil="especialista"
        )
        await _rodar_com(
            cliente,
            h,
            _def_particionada(
                [{"origem": "coluna", "nome": "PROJECTID", "chave": "project_id"}],
                "SELECT * FROM (VALUES (1201),(1202)) t(PROJECTID)",
                "ds_index",
            ),
            {"data_registro": "2026-08-20", "operacao_id": "61"},
        )
        indice = json.loads(
            (tmp_path / "lake" / "gold" / "dev" / "ds_index" / "_atual.json").read_text()
        )
        assert indice["chaves"] == ["project_id"]  # the consumer discovers the path format
        escopos = {tuple(sorted(p["particao"].items())) for p in indice["particoes"]}
        assert escopos == {(("project_id", "1201"),), (("project_id", "1202"),)}
        assert all(p["caminho"].endswith("dados.parquet") for p in indice["particoes"])
    finally:
        _limpar_settings()


async def test_manifesto_carrega_os_parametros_da_execucao(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """The manifest answers "where did this number come from?" for whoever only has the Parquet. It
    used to carry no parameter at all - not even the record date that defines the data snapshot."""
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(_lake(tmp_path)))
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="par4@example.com", perfil="especialista"
        )
        det = await _rodar_com(
            cliente,
            h,
            _def_particionada(
                [{"origem": "parametro", "nome": "operacao_id"}],
                "SELECT 1 AS n",
                "ds_manifest",
            ),
            {"data_registro": "2026-08-20", "operacao_id": "61"},
        )
        assert det["status"] == "concluida"
        escopo = tmp_path / "lake" / "gold" / "dev" / "ds_manifest" / "operacao_id=61"
        eid = json.loads((escopo / "_atual.json").read_text())["execucao_id"]
        man = json.loads((escopo / f"execucao_id={eid}" / "_manifesto.json").read_text())
        assert man["parametros"]["operacao_id"] == "61"
        assert man["parametros"]["data_registro"] == "2026-08-20"
        assert man["particao"] == {"operacao_id": "61"}
    finally:
        _limpar_settings()


async def test_duas_corridas_no_mesmo_dia_nao_se_perdem(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """More than one parquet per day is the normal case (reload, retry). Both runs coexist under the
    SAME scope and that scope pointer points at the current one - the previous one stays reachable
    by path, it does not become an ownerless orphan."""
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(_lake(tmp_path)))
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="par5@example.com", perfil="especialista"
        )
        definicao = _def_particionada(
            [
                {"origem": "parametro", "nome": "data_registro"},
                {"origem": "parametro", "nome": "operacao_id"},
            ],
            "SELECT 1 AS n",
            "ds_two",
        )
        params = {"data_registro": "2026-08-20", "operacao_id": "61"}
        d1 = await _rodar_com(cliente, h, definicao, params, nome="Two A")
        d2 = await _rodar_com(cliente, h, definicao, params, nome="Two B")
        assert d1["status"] == d2["status"] == "concluida"
        escopo = (
            tmp_path
            / "lake"
            / "gold"
            / "dev"
            / "ds_two"
            / "data_registro=2026-08-20"
            / "operacao_id=61"
        )
        corridas = sorted(p.name for p in escopo.glob("execucao_id=*"))
        assert len(corridas) == 2, "the 2nd run of the day cannot erase the 1st"
        assert (
            f"execucao_id={json.loads((escopo / '_atual.json').read_text())['execucao_id']}"
            in corridas
        )
    finally:
        _limpar_settings()


async def test_particao_so_vale_no_destino_lake(owner_engine, cliente):
    """Same rule as `camada`: a config with no effect is REFUSED, not ignored."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="par6@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "Unwanted partition"}, headers=h)).json()[
        "id"
    ]
    definicao = {
        "nos": [
            {"id": "src", "tipo": "sql", "sql": "SELECT 1 AS n"},
            {
                "id": "out",
                "tipo": "saida",
                "config": {
                    "nome": "ds_x",
                    "particao": [{"origem": "coluna", "nome": "n"}],
                },
            },
        ],
        "arestas": [{"de": "src", "para": "out"}],
    }
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 422
    assert r.json()["erro"]["codigo"] == "grafo_invalido"


async def test_particao_por_parametro_nao_declarado_recusada(owner_engine, cliente):
    """Partitioning by a parameter the pipeline does not declare would publish every day to the same
    empty `<key>=` path - an error that would only show up months later, in the bucket."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="par7@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "Ghost param"}, headers=h)).json()["id"]
    definicao = _def_particionada(
        [{"origem": "parametro", "nome": "competencia"}],  # not declared
        "SELECT 1 AS n",
        "ds_ghost",
    )
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 422
    assert r.json()["erro"]["codigo"] == "grafo_invalido"


async def test_particao_por_coluna_ausente_falha_a_execucao(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """A column that does not exist in the result must bring the node down with a clear error.
    Publishing anyway would create a single partition, with a wrong name, looking like good data."""
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(_lake(tmp_path)))
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="par8@example.com", perfil="especialista"
        )
        det = await _rodar_com(
            cliente,
            h,
            _def_particionada(
                [{"origem": "coluna", "nome": "DOES_NOT_EXIST"}],
                "SELECT 1 AS n",
                "ds_missing_column",
            ),
            {"data_registro": "2026-08-20", "operacao_id": "61"},
        )
        assert det["status"] in ("falha", "erro")
        assert not (tmp_path / "lake" / "gold" / "dev" / "ds_missing_column").exists()
    finally:
        _limpar_settings()


async def test_valor_de_particao_com_barra_recusado(owner_engine, cliente, tmp_path, monkeypatch):
    """The value becomes a PATH in the bucket: `/` or `..` in the content would write outside the
    dataset scope. A loud refusal, never silent sanitising (the data would come out mislabelled)."""
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(_lake(tmp_path)))
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="par9@example.com", perfil="especialista"
        )
        det = await _rodar_com(
            cliente,
            h,
            _def_particionada(
                [{"origem": "coluna", "nome": "PROJECTID", "chave": "project_id"}],
                "SELECT * FROM (VALUES ('../outside')) t(PROJECTID)",
                "ds_slash",
            ),
            {"data_registro": "2026-08-20", "operacao_id": "61"},
        )
        assert det["status"] in ("falha", "erro")
    finally:
        _limpar_settings()


# --------------------------------------------------------------------------------------------
# Hardening of the partition (adversarial refutation of the design).
# Each test below was born RED against the first implementation - the list of defects it had is in
# each docstring, because that is what keeps the defect from coming back.
# --------------------------------------------------------------------------------------------


def _lista_ponteiros(raiz) -> list[str]:
    """Every `_atual.json` below a dataset - the scope pointer is the *commit point*, so counting them
    is the only way to ask 'what did this run leave visible?'."""
    return sorted(str(p.relative_to(raiz)) for p in raiz.rglob("_atual.json"))


async def test_execucao_que_falha_devolve_o_id_da_execucao(owner_engine, cliente):
    """`POST /executar` creates the run BEFORE running; if a node fails, that is a BUSINESS outcome,
    not a request error - the run exists, has a trail and must be openable.

    With the workflow engine (the default) the route always returns 200 + id and the failure shows
    up in `GET /execucoes/{id}`. Inline it used to return a 400 envelope with no id at all: the same
    API gave responses of different shapes depending on the execution mode, and whoever failed was
    left with no path to their own trail."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="rid1@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "Failure"}, headers=h)).json()["id"]
    definicao = {
        "nos": [{"id": "src", "tipo": "sql", "sql": "SELECT * FROM does_not_exist_anywhere"}],
        "arestas": [],
    }
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    r = await cliente.post(f"/pipelines/{pid}/executar", headers=h)
    assert r.status_code == 200, "the run was created: its id cannot vanish with the failure"
    corpo = r.json()
    assert corpo.get("id"), "without the id there is no path to the trail - what this test protects"

    # The OUTCOME arrives by different paths depending on the mode, and the test must accept both:
    # inline the route already returns `falha` in the body; with the workflow engine (the default)
    # it returns `executando` at once and the failure shows up later in `GET /execucoes/{id}`.
    # Requiring `falha` in the body would tie the test to inline mode and make it fail in the
    # production configuration, which is precisely the one that needs coverage.
    if corpo["status"] == "falha":
        assert corpo["erro"]
        det = (await cliente.get(f"/execucoes/{corpo['id']}", headers=h)).json()
    else:
        assert corpo["status"] == "executando", corpo
        det = await aguardar_execucao(cliente, corpo["id"], h)
    assert det["status"] == "falha", det


async def test_falha_em_um_escopo_nao_deixa_nenhum_ponteiro_avancar(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """ONE scope that fails to write must leave the bucket as it was - no pointer moved, not even
    those of the projects that wrote fine.

    The defect: `asyncio.gather` without `return_exceptions` propagates the 1st exception and does NOT
    cancel the siblings. The node was marked 'falha', the run index was never written, and seconds
    later the other 189 projects finished moving their `_atual.json` in the background - the run
    was recorded as failed and the bucket said otherwise. A single 5xx from GCS on one project is
    enough to trigger it."""
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(_lake(tmp_path)))
    try:
        from cflow_core.ingestao.armazenamento import ArmazenamentoLocal

        # The failure is injected into BOTH ways the data reaches the lake - `gravar` (bytes) and
        # `gravar_arquivo` (by link, the output publishing path). The property under test belongs
        # to the FAN-OUT, not to which primitive writes: probing only one makes the test pass by
        # never having fired anything, which is the worst way for a test like this to go green.
        original = ArmazenamentoLocal.gravar
        original_arquivo = ArmazenamentoLocal.gravar_arquivo

        def _bad_project(caminho: str) -> bool:
            return "project_id=1203/" in caminho and caminho.endswith("dados.parquet")

        async def _write_with_one_bad_project(self, camada, caminho, dados, **kw):
            if _bad_project(caminho):
                raise OSError("network dropped in the middle of the fan-out")
            return await original(self, camada, caminho, dados, **kw)

        async def _write_file_with_one_bad_project(self, camada, caminho, origem, **kw):
            if _bad_project(caminho):
                raise OSError("network dropped in the middle of the fan-out")
            return await original_arquivo(self, camada, caminho, origem, **kw)

        monkeypatch.setattr(ArmazenamentoLocal, "gravar", _write_with_one_bad_project)
        monkeypatch.setattr(ArmazenamentoLocal, "gravar_arquivo", _write_file_with_one_bad_project)
        _, h = await usuario_logado(
            owner_engine, cliente, email="atom1@example.com", perfil="especialista"
        )
        det = await _rodar_com(
            cliente,
            h,
            _def_particionada(
                [{"origem": "coluna", "nome": "PROJECTID", "chave": "project_id"}],
                "SELECT * FROM (VALUES (1201),(1202),(1203),(1204),(1205)) t(PROJECTID)",
                "ds_atomic",
            ),
            {"data_registro": "2026-08-20", "operacao_id": "61"},
        )
        assert det["status"] in ("falha", "erro")
        raiz = tmp_path / "lake" / "gold" / "dev" / "ds_atomic"
        assert _lista_ponteiros(raiz) == [], (
            "a scope published in a failed run is half a dataset dressed as a whole one"
        )
        # And it must not appear LATER: proof that no sibling stayed alive on the event loop.
        await asyncio.sleep(0.5)
        assert _lista_ponteiros(raiz) == [], (
            "pointer moved after the node was already marked as failed"
        )
    finally:
        _limpar_settings()


async def test_muitas_combinacoes_no_mesmo_escopo_nao_viram_sql_quadratico(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """A date/time column collapses to the DAY: N rows with N distinct times are ONE scope, with N raw
    combinations. The COPY filter was an OR of equalities, one term per combination - megabytes of
    SQL and time growing with the square of the rows (measured: 2x rows -> 4.6x time).

    Here this is a normal case (`{"origem":"coluna","nome":"event_time"}`), not a hostile value."""
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(_lake(tmp_path)))
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="quad1@example.com", perfil="especialista"
        )
        n = 40000
        sql = (
            "SELECT TIMESTAMP '2026-08-20 00:00:00' + INTERVAL (i) MILLISECOND AS momento, "
            f"i AS valor FROM range({n}) t(i)"
        )
        t0 = time.monotonic()
        det = await _rodar_com(
            cliente,
            h,
            _def_particionada(
                [{"origem": "coluna", "nome": "momento", "chave": "dia"}], sql, "ds_quadratic"
            ),
            {"data_registro": "2026-08-20", "operacao_id": "61"},
        )
        gasto = time.monotonic() - t0
        assert det["status"] == "concluida"
        escopo = tmp_path / "lake" / "gold" / "dev" / "ds_quadratic" / "dia=2026-08-20"
        ponteiro = json.loads((escopo / "_atual.json").read_text())
        assert ponteiro["linhas"] == n, "the whole day is ONE scope, with all the rows inside"
        arq = escopo / f"execucao_id={ponteiro['execucao_id']}" / "dados.parquet"
        lidas = duckdb.connect().execute(f"SELECT count(*) FROM read_parquet('{arq}')").fetchone()
        assert lidas[0] == n
        # Large margin on purpose: what is measured is the CLASS (linear x quadratic), not the clock.
        # With the OR per combination, this case took tens of seconds in the COPY alone.
        assert gasto < 30, (
            f"publishing took {gasto:.1f}s - the filter is growing with the rows again"
        )
    finally:
        _limpar_settings()


async def test_valor_de_particao_longo_demais_recusado(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """A column value with no size cap becomes a path, and what rejects it is the file system
    `open()` - with scopes already published, a raw `OSError [Errno 63] File name too long` on the
    specialist screen and the absolute lake path inside the message.

    Worse: the limit DIVERGES by backend (255 bytes per segment on POSIX, 1024 for the GCS object
    name), so the same pipeline would be accepted in one place and refused in the other."""
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(_lake(tmp_path)))
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="long1@example.com", perfil="especialista"
        )
        det = await _rodar_com(
            cliente,
            h,
            _def_particionada(
                [{"origem": "coluna", "nome": "PROJECTID", "chave": "project_id"}],
                f"SELECT * FROM (VALUES ('{'A' * 300}'),('1202')) t(PROJECTID)",
                "ds_long",
            ),
            {"data_registro": "2026-08-20", "operacao_id": "61"},
        )
        assert det["status"] in ("falha", "erro")
        erro = " ".join(str(n.get("erro") or "") for n in det["nos"]) + str(det.get("erro") or "")
        assert "too long" in erro, erro
        assert str(tmp_path) not in erro, "the error message does not expose the absolute lake path"
        assert not (tmp_path / "lake" / "gold" / "dev" / "ds_long").exists(), (
            "the loud refusal happens BEFORE the 1st write - no scope published"
        )
    finally:
        _limpar_settings()


async def test_escopos_que_so_diferem_na_caixa_sao_recusados(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """`acme` and `ACME` in the same run: on GCS they are two scopes; on the local lake (APFS, the
    macOS default and the air-gapped mode) they are one - the second hit immutability with
    `objeto_ja_existe` ("the layer is immutable"), an INFRASTRUCTURE error for a DATA condition, and
    even left a partial write. Mixed-case project codes are the normal case here."""
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(_lake(tmp_path)))
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="caixa1@example.com", perfil="especialista"
        )
        det = await _rodar_com(
            cliente,
            h,
            _def_particionada(
                [{"origem": "coluna", "nome": "PROJECTID", "chave": "project_id"}],
                "SELECT * FROM (VALUES ('acme'),('ACME'),('beta')) t(PROJECTID)",
                "ds_case",
            ),
            {"data_registro": "2026-08-20", "operacao_id": "61"},
        )
        assert det["status"] in ("falha", "erro")
        erro = " ".join(str(n.get("erro") or "") for n in det["nos"]) + str(det.get("erro") or "")
        assert "case" in erro.lower(), erro
        assert not (tmp_path / "lake" / "gold" / "dev" / "ds_case").exists()
    finally:
        _limpar_settings()


async def test_ponteiro_de_um_escopo_nunca_aponta_o_dado_de_outra_caixa(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """Two RUNS, `acme` and then `ACME`. On a case-sensitive filesystem they are two legitimate
    scopes; on a case-insensitive one the second aliased the first SILENTLY - whoever asked for the
    dataset of project `acme` received the data of `ACME`, with no error. The invariant that holds on
    both is that the pointer of one scope never describes another."""
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(_lake(tmp_path)))
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="caixa2@example.com", perfil="especialista"
        )
        definicao_a = _def_particionada(
            [{"origem": "coluna", "nome": "PROJECTID", "chave": "project"}],
            "SELECT * FROM (VALUES ('acme')) t(PROJECTID)",
            "ds_case2",
        )
        definicao_b = _def_particionada(
            [{"origem": "coluna", "nome": "PROJECTID", "chave": "project"}],
            "SELECT * FROM (VALUES ('ACME')) t(PROJECTID)",
            "ds_case2",
        )
        params = {"data_registro": "2026-08-20", "operacao_id": "61"}
        d1 = await _rodar_com(cliente, h, definicao_a, params, nome="Case A")
        assert d1["status"] == "concluida"
        d2 = await _rodar_com(cliente, h, definicao_b, params, nome="Case B")
        base = tmp_path / "lake" / "gold" / "dev" / "ds_case2"
        if d2["status"] == "concluida":
            # Case-sensitive FS: two scopes, each with its own data.
            assert (base / "project=acme" / "_atual.json").is_file()
            assert (base / "project=ACME" / "_atual.json").is_file()
            assert json.loads((base / "project=acme" / "_atual.json").read_text())["particao"] == {
                "project": "acme"
            }
        else:
            # Case-insensitive FS: a loud refusal. What cannot happen is the 'acme' pointer starting
            # to describe 'ACME'.
            assert json.loads((base / "project=acme" / "_atual.json").read_text())["particao"] == {
                "project": "acme"
            }
    finally:
        _limpar_settings()


async def test_indice_da_corrida_sobrevive_a_corrida_seguinte(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """`<dataset>/_atual.json` is MUTABLE: the next run of ANY unit overwrites it, and with it goes the
    only evidence of which scopes the previous run covered - exactly the consequence the partition
    design exists to kill, surviving in the index.

    Since the datasets are published per unit, after a nightly cycle the index would describe only
    the last one to finish. A reconciliation check must be repeatable on a past date: each run must
    leave an IMMUTABLE index next to the data it describes."""
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(_lake(tmp_path)))
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="idx1@example.com", perfil="especialista"
        )

        def _def(op_sql: str) -> dict:
            return _def_particionada(
                [
                    {"origem": "parametro", "nome": "operacao_id"},
                    {"origem": "coluna", "nome": "PROJECTID", "chave": "project_id"},
                ],
                op_sql,
                "ds_index_hist",
            )

        d1 = await _rodar_com(
            cliente,
            h,
            _def("SELECT * FROM (VALUES (1201),(1202),(1203)) t(PROJECTID)"),
            {"data_registro": "2026-08-20", "operacao_id": "61"},
            nome="Index 61",
        )
        assert d1["status"] == "concluida"
        d2 = await _rodar_com(
            cliente,
            h,
            _def("SELECT * FROM (VALUES (9001)) t(PROJECTID)"),
            {"data_registro": "2026-08-21", "operacao_id": "62"},
            nome="Index 62",
        )
        assert d2["status"] == "concluida"
        base = tmp_path / "lake" / "gold" / "dev" / "ds_index_hist"
        # The mutable index is already the unit 62 one - which is expected ("what is current").
        assert json.loads((base / "_atual.json").read_text())["execucao_id"] == d2["id"]
        # The unit 61 one is still reachable, under the parameter prefix of that run.
        historico = base / "operacao_id=61" / f"execucao_id={d1['id']}" / "_indice.json"
        assert historico.is_file(), "a run without an immutable index is an irreproducible run"
        corpo = json.loads(historico.read_text())
        assert corpo["execucao_id"] == d1["id"]
        assert {p["particao"]["project_id"] for p in corpo["particoes"]} == {"1201", "1202", "1203"}
    finally:
        _limpar_settings()


async def test_leitura_pelo_indice_e_exata_e_o_glob_hive_infla(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """The path LOOKS like a hive table and invites `read_parquet('<dataset>/**/dados.parquet')` -
    which sums EVERY run of the scope, because `execucao_id=` sits in the middle of the path and
    becomes just one more partition column. Two runs of the same day (reload, retry) double the
    total, silently, looking right.

    The layout does not change (the contract requires both runs to coexist): what exists is the
    INDEX, and it is the valid read. This test pins both halves so that the documentation is not
    alone in holding up the difference."""
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(_lake(tmp_path)))
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="glob1@example.com", perfil="especialista"
        )
        definicao = _def_particionada(
            [{"origem": "coluna", "nome": "PROJECTID", "chave": "project_id"}],
            "SELECT * FROM (VALUES (1201),(1201),(1202)) t(PROJECTID)",
            "ds_glob",
        )
        params = {"data_registro": "2026-08-20", "operacao_id": "61"}
        await _rodar_com(cliente, h, definicao, params, nome="Glob A")
        await _rodar_com(cliente, h, definicao, params, nome="Glob B")
        base = tmp_path / "lake" / "gold" / "dev" / "ds_glob"
        con = duckdb.connect()
        inflado = con.execute(
            f"SELECT count(*) FROM read_parquet('{base}/**/dados.parquet', hive_partitioning=1)"
        ).fetchone()[0]
        assert inflado == 6, "the glob sums both runs - which is why it is not the read"
        indice = json.loads((base / "_atual.json").read_text())
        raiz_lake = tmp_path / "lake" / "gold"
        arquivos = [str(raiz_lake / p["caminho"]) for p in indice["particoes"]]
        exato = con.execute("SELECT count(*) FROM read_parquet(?)", [arquivos]).fetchone()[0]
        assert exato == 3, "through the index, the total is that of the current run"
        assert sum(p["linhas"] for p in indice["particoes"]) == 3
    finally:
        _limpar_settings()


async def test_manifesto_guarda_o_parametro_como_ele_e(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """A parameter that does NOT become a path segment must go into the manifest as it is. It used to
    go through the same normalisation as the path - a regex that truncates any timestamp-looking
    text to the day -, so the cut-off time vanished from an IMMUTABLE object, which cannot be fixed
    afterwards.

    The scope is still described, unambiguously, in `particao` (which is what the path says)."""
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(_lake(tmp_path)))
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="manif1@example.com", perfil="especialista"
        )
        definicao = {
            "parametros": [
                {"nome": "data_registro", "tipo": "as_of", "default": "hoje"},
                {"nome": "operacao_id", "tipo": "texto", "obrigatorio": True},
                {"nome": "corte", "tipo": "texto"},
            ],
            "nos": [
                {"id": "src", "tipo": "sql", "sql": "SELECT 1 AS n"},
                {
                    "id": "out",
                    "tipo": "saida",
                    "config": {
                        "destino": "lake",
                        "camada": "gold",
                        "dataset": "ds_verbatim",
                        "particao": [{"origem": "parametro", "nome": "operacao_id"}],
                    },
                },
            ],
            "arestas": [{"de": "src", "para": "out"}],
        }
        det = await _rodar_com(
            cliente,
            h,
            definicao,
            {
                "data_registro": "2026-08-20",
                "operacao_id": "61",
                "corte": "2026-08-20 23:59:59",
            },
        )
        assert det["status"] == "concluida"
        escopo = tmp_path / "lake" / "gold" / "dev" / "ds_verbatim" / "operacao_id=61"
        eid = json.loads((escopo / "_atual.json").read_text())["execucao_id"]
        man = json.loads((escopo / f"execucao_id={eid}" / "_manifesto.json").read_text())
        assert man["parametros"]["corte"] == "2026-08-20 23:59:59", (
            "the cut-off time cannot be truncated in an immutable object"
        )
        assert man["parametros"]["data_registro"] == "2026-08-20"
        assert man["particao"] == {"operacao_id": "61"}
    finally:
        _limpar_settings()


async def test_temporario_do_destino_arquivo_e_por_execucao(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """Two runs of the same pipeline writing the same `nome` wrote to the SAME temp file
    (`.{nome}.parquet.tmp`): the second COPY went on top of the first and the first `os.replace`
    published mixed bytes. The temp file carries the `execucao_id`; and the node snapshot also goes
    through a temp file + rename (whoever reads through the shared volume never sees half a file).
    """
    import cflow_core.execucao.engine as eng

    _, h = await usuario_logado(
        owner_engine, cliente, email="sai-tmp@example.com", perfil="especialista"
    )
    destino = str(tmp_path / "export")
    definicao = {
        "nos": [
            {"id": "src", "tipo": "sql", "sql": "SELECT 1 AS n"},
            {"id": "out", "tipo": "saida", "config": {"nome": "ds_tmp", "caminho": destino}},
        ],
        "arestas": [{"de": "src", "para": "out"}],
    }
    renomes: list[tuple[str, str]] = []
    original = eng.os.replace

    def _capturar(a, b):
        renomes.append((str(a), str(b)))
        return original(a, b)

    monkeypatch.setattr(eng.os, "replace", _capturar)

    det = await _rodar(cliente, h, definicao)
    assert det["status"] == "concluida"

    finais = {b: a for a, b in renomes}
    arquivo = os.path.join(destino, "ds_tmp.parquet")
    assert arquivo in finais, renomes
    assert det["id"] in finais[arquivo]  # temp file stamped with the run
    # snapshots of the two nodes: written to a temp file alongside and renamed to `<no_id>.parquet`
    snaps = [b for b in finais if b.endswith("src.parquet") or b.endswith("out.parquet")]
    assert len(snaps) == 2, renomes
    for b in snaps:
        assert finais[b].endswith(".tmp") and os.path.dirname(finais[b]) == os.path.dirname(b)
    # no temp file left
    leftovers = [p for p in os.listdir(destino) if p.endswith(".tmp")]
    assert leftovers == []


async def test_indice_corrente_junta_as_particoes_de_corridas_diferentes(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """`<dataset>/_atual.json` is the CURRENT index per scope, not "the one of the last run": with a
    per-unit fan-out, N runs of the same night publish different partitions of the same dataset, and
    each one overwriting the whole index left only the one that finished last. The scope of this run
    replaces its namesake; the others stay."""
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(_lake(tmp_path)))
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="idx-merge@example.com", perfil="especialista"
        )

        def _def(op_sql: str) -> dict:
            return _def_particionada(
                [
                    {"origem": "parametro", "nome": "operacao_id"},
                    {"origem": "coluna", "nome": "PROJECTID", "chave": "project_id"},
                ],
                op_sql,
                "ds_index_merge",
            )

        d61 = await _rodar_com(
            cliente,
            h,
            _def("SELECT * FROM (VALUES (1),(2)) t(PROJECTID)"),
            {"data_registro": "2026-08-20", "operacao_id": "61"},
            nome="Merge 61",
        )
        d62 = await _rodar_com(
            cliente,
            h,
            _def("SELECT * FROM (VALUES (9)) t(PROJECTID)"),
            {"data_registro": "2026-08-20", "operacao_id": "62"},
            nome="Merge 62",
        )
        d61b = await _rodar_com(
            cliente,
            h,
            _def("SELECT * FROM (VALUES (1)) t(PROJECTID)"),
            {"data_registro": "2026-08-21", "operacao_id": "61"},
            nome="Merge 61 again",
        )
        assert d61["status"] == d62["status"] == d61b["status"] == "concluida"
        base = tmp_path / "lake" / "gold" / "dev" / "ds_index_merge"
        indice = json.loads((base / "_atual.json").read_text())
        assert indice["execucao_id"] == d61b["id"]  # header: whoever touched it last
        por_escopo = {
            (p["particao"]["operacao_id"], p["particao"]["project_id"]): p["caminho"]
            for p in indice["particoes"]
        }
        # Unit 62 did NOT vanish when 61 came back; project 1 of 61 points to the new run; project 2
        # of 61 (which the 2nd run did not produce) keeps the pointer of the 1st run.
        assert f"execucao_id={d62['id']}" in por_escopo[("62", "9")]
        assert f"execucao_id={d61b['id']}" in por_escopo[("61", "1")]
        assert f"execucao_id={d61['id']}" in por_escopo[("61", "2")]
        # The IMMUTABLE index of each run is still just its own.
        proprio = json.loads(
            (base / "operacao_id=61" / f"execucao_id={d61b['id']}" / "_indice.json").read_text()
        )
        assert [p["particao"]["project_id"] for p in proprio["particoes"]] == ["1"]
    finally:
        _limpar_settings()
