"""Reading a published dataset BY RUN - `GET /publicados/{camada}/{dataset}/...`.

`_atual.json` answers "what is valid now"; closing needs to answer "what did THIS run publish", and
the next run of the same scope moves the pointer. The read here is deterministic from the run:
executed version -> `saida` node -> immutable index of that run. Never a glob, never the current
pointer.

Lake under `tmp` (CFLOW_LAKE_DIR), the same arrangement as `test_saida.py`.
"""

from __future__ import annotations

import json
import uuid

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao, contrato_ambiente_gold

_AMBIENTE_POR_PIPELINE: set[str] = set()


def _limpar_settings() -> None:
    from cflow_core.config import get_settings

    get_settings.cache_clear()


def _lake(tmp_path, monkeypatch):
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    _limpar_settings()
    return tmp_path / "lake"


async def _criar_publicado(cliente, h, definicao: dict, nome: str) -> str:
    definicao = contrato_ambiente_gold(definicao)
    pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    if any(p.get("nome") == "ambiente" for p in definicao.get("parametros") or []):
        _AMBIENTE_POR_PIPELINE.add(pid)
    return pid


async def _executar(cliente, h, pid: str, parametros: dict | None = None) -> str:
    parametros = dict(parametros or {})
    if pid in _AMBIENTE_POR_PIPELINE:
        parametros.setdefault("ambiente", "dev")
    corpo = {"parametros": parametros}
    ex = (await cliente.post(f"/pipelines/{pid}/executar", json=corpo, headers=h)).json()
    det = await aguardar_execucao(cliente, ex["id"], h)
    assert det["status"] == "concluida", det
    return ex["id"]


# Partition by PARAMETER (`operacao_id`) and by COLUMN (`PROJECTID` -> `project_id`). The `lote`
# changes the data between runs of the SAME scope; in run "B" only project 1201 comes out, so the
# 1201 pointer is moved and the 1202 one stays with run "A".
_SQL_PROJECTS = (
    "SELECT PROJECTID, valor, CAST(:lote AS VARCHAR) AS lote "
    "FROM (VALUES (1201, 10), (1201, 20), (1202, 30)) t(PROJECTID, valor) "
    "WHERE CAST(:lote AS VARCHAR) = 'A' OR PROJECTID = 1201"
)


def _def_projects(dataset: str) -> dict:
    return {
        "parametros": [
            {"nome": "operacao_id", "tipo": "texto", "obrigatorio": True},
            {"nome": "lote", "tipo": "texto", "obrigatorio": True},
        ],
        "nos": [
            {"id": "src", "tipo": "sql", "sql": _SQL_PROJECTS},
            {
                "id": "out",
                "tipo": "saida",
                "config": {
                    "destino": "lake",
                    "camada": "gold",
                    "dataset": dataset,
                    "particao": [
                        {"origem": "parametro", "nome": "operacao_id"},
                        {"origem": "coluna", "nome": "PROJECTID", "chave": "project_id"},
                    ],
                },
            },
        ],
        "arestas": [{"de": "src", "para": "out"}],
    }


def _def_sem_particao(dataset: str) -> dict:
    return {
        "nos": [
            {
                "id": "src",
                "tipo": "sql",
                "sql": (
                    "SELECT CAST(range AS INTEGER) AS n, CAST(0.1 AS DOUBLE) AS v, "
                    "CAST(1.25 AS DECIMAL(18,2)) AS d FROM range(25)"
                ),
            },
            {
                "id": "out",
                "tipo": "saida",
                "config": {"destino": "lake", "camada": "silver", "dataset": dataset},
            },
        ],
        "arestas": [{"de": "src", "para": "out"}],
    }


async def _duas_corridas(owner_engine, cliente, email: str, dataset: str):
    _, h = await usuario_logado(owner_engine, cliente, email=email, perfil="especialista")
    pid = await _criar_publicado(cliente, h, _def_projects(dataset), "Published projects")
    e1 = await _executar(cliente, h, pid, {"operacao_id": "61", "lote": "A"})
    e2 = await _executar(cliente, h, pid, {"operacao_id": "61", "lote": "B"})
    return h, pid, e1, e2


def _dados(dataset: str, camada: str = "gold") -> str:
    return f"/publicados/{camada}/{dataset}/dados?ambiente=dev"


async def test_le_a_corrida_pedida_mesmo_depois_de_o_ponteiro_andar(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """(a) The 2nd run moves the scope `_atual.json`; reading by the 1st `execucao_id` still returns
    ITS data - which is what makes the closing repeatable."""
    lake = _lake(tmp_path, monkeypatch)
    try:
        h, _, e1, e2 = await _duas_corridas(owner_engine, cliente, "pub1@example.com", "pub_a")
        escopo = lake / "gold" / "dev" / "pub_a" / "operacao_id=61" / "project_id=1201"
        assert json.loads((escopo / "_atual.json").read_text())["execucao_id"] == e2, (
            "precondition: the 2nd run must have moved the scope pointer"
        )

        r1 = await cliente.get(
            _dados("pub_a"),
            params={"execucao_id": e1, "ordenar": ["PROJECTID", "valor"]},
            headers=h,
        )
        assert r1.status_code == 200, r1.text
        c1 = r1.json()
        assert c1["execucao_id"] == e1
        assert c1["dataset"] == "pub_a" and c1["camada"] == "gold"
        assert c1["total"] == 3
        assert {linha["lote"] for linha in c1["linhas"]} == {"A"}
        assert [(li["PROJECTID"], li["valor"]) for li in c1["linhas"]] == [
            (1201, 10),
            (1201, 20),
            (1202, 30),
        ]
        assert {p["particao"]["project_id"]: p["linhas"] for p in c1["particoes"]} == {
            "1201": 2,
            "1202": 1,
        }
        assert {c["nome"] for c in c1["colunas"]} == {"PROJECTID", "valor", "lote"}

        c2 = (await cliente.get(_dados("pub_a"), params={"execucao_id": e2}, headers=h)).json()
        assert c2["total"] == 2
        assert {linha["lote"] for linha in c2["linhas"]} == {"B"}
    finally:
        _limpar_settings()


async def test_dataset_da_familia_com_BARRA_e_enderecavel_cru_e_codificado(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """Gold groups by family (`ref/01/summary`, `load/orders`): the dataset name contains `/`. With
    `{dataset}` lacking the `path` converter, the route did not match - ASGI delivers the DECODED
    path, so `ref%2F01%2Fsummary` becomes three segments and gave a 404 before any handler."""
    _lake(tmp_path, monkeypatch)
    try:
        h, _, e1, _ = await _duas_corridas(
            owner_engine, cliente, "pubbarra@example.com", "ref/01/pub_barra"
        )
        for caminho in (
            "/publicados/gold/ref/01/pub_barra/dados",
            "/publicados/gold/ref%2F01%2Fpub_barra/dados",
        ):
            r = await cliente.get(caminho, params={"ambiente": "dev", "execucao_id": e1}, headers=h)
            assert r.status_code == 200, (caminho, r.text)
            assert r.json()["dataset"] == "ref/01/pub_barra"
            assert r.json()["total"] == 3

        ri = await cliente.get(
            "/publicados/gold/ref%2F01%2Fpub_barra/indice",
            params={"ambiente": "dev", "execucao_id": e1},
            headers=h,
        )
        assert ri.status_code == 200, ri.text
    finally:
        _limpar_settings()


async def test_filtro_de_particao_por_coluna_e_por_parametro(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """A COLUMN key filters the index partitions; a PARAMETER key is checked against the run itself -
    another value is asking the wrong run, not "zero rows"."""
    _lake(tmp_path, monkeypatch)
    try:
        h, _, e1, _ = await _duas_corridas(owner_engine, cliente, "pub2@example.com", "pub_f")
        r = await cliente.get(
            _dados("pub_f"),
            params={"execucao_id": e1, "operacao_id": "61", "project_id": "1202"},
            headers=h,
        )
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 1
        assert [p["particao"]["project_id"] for p in r.json()["particoes"]] == ["1202"]

        errada = await cliente.get(
            _dados("pub_f"), params={"execucao_id": e1, "operacao_id": "62"}, headers=h
        )
        assert errada.status_code == 404
        assert errada.json()["erro"]["codigo"] == "publicado_ausente"

        estranha = await cliente.get(
            _dados("pub_f"), params={"execucao_id": e1, "company": "7"}, headers=h
        )
        assert estranha.status_code == 400
        assert estranha.json()["erro"]["codigo"] == "filtro_particao_invalido"
    finally:
        _limpar_settings()


async def test_pagina_total_e_soma_decimal(owner_engine, cliente, tmp_path, monkeypatch):
    """(b) skip/take/total and (d) DECIMAL sum over the filtered set - without a partition, the
    path is the run manifest."""
    _lake(tmp_path, monkeypatch)
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="pub3@example.com", perfil="especialista"
        )
        pid = await _criar_publicado(cliente, h, _def_sem_particao("pub_b"), "No partition")
        eid = await _executar(cliente, h, pid)

        r = await cliente.get(
            _dados("pub_b", "silver"),
            params={
                "execucao_id": eid,
                "skip": 10,
                "take": 5,
                "ordenar": "n",
                "agregar": "sum:v,d",
            },
            headers=h,
        )
        assert r.status_code == 200, r.text
        corpo = r.json()
        assert corpo["total"] == 25
        assert corpo["skip"] == 10 and corpo["take"] == 5
        assert [linha["n"] for linha in corpo["linhas"]] == [10, 11, 12, 13, 14]
        # 0.1 x 25 in DOUBLE is 2.5000000000000004; in DECIMAL it is exact - and comes as TEXT.
        assert corpo["agregados"] == {"sum": {"v": "2.5000000000", "d": "31.25"}}
        assert corpo["linhas"][0]["d"] == "1.25"  # the row DECIMAL is also text
        assert corpo["particoes"] == [{"particao": {}, "linhas": 25}]

        desc = (
            await cliente.get(
                _dados("pub_b", "silver"),
                params={
                    "execucao_id": eid,
                    "ordenar": "-n",
                    "take": 2,
                    "filtro": "n~1",
                    "agregar": ["sum:n", "count_distinct:n,v"],
                },
                headers=h,
            )
        ).json()
        # "contains 1": 1, 10-19 and 21 -> 12 rows; in descending order the first two are 21, 19.
        assert desc["total"] == 12
        assert [linha["n"] for linha in desc["linhas"]] == [21, 19]
        # The same column summed AND counted, each under its own function: 1 + (10..19) + 21 = 167.
        assert desc["agregados"] == {
            "sum": {"n": "167"},
            "count_distinct": {"n": "12", "v": "1"},
        }

        fora = await cliente.get(
            _dados("pub_b", "silver"), params={"execucao_id": eid, "take": 1001}, headers=h
        )
        assert fora.status_code == 400
        assert fora.json()["erro"]["codigo"] == "paginacao_invalida"
    finally:
        _limpar_settings()


# The EXACT shape of reference gold: partition by PARAMETER only, one of them of type `competencia`
# - which does not reach the engine raw (it becomes `competencia_inicio`/`_fim`) and enters the path
# through day 1. This is the path executed version -> types -> `_inicio` -> `_prefixo_imutavel` ->
# `_indice.json`, which used to be covered only in pieces, by pure tests.
_PARAMETROS_GOLD = [
    {"nome": "operacao_id", "tipo": "texto", "obrigatorio": True},
    {"nome": "competencia", "tipo": "competencia", "default": "atual"},
]
_PARTICAO_GOLD = [
    {"origem": "parametro", "nome": "operacao_id"},
    {"origem": "parametro", "nome": "competencia"},
]


def _def_gold_competencia(dataset: str) -> dict:
    return {
        "parametros": _PARAMETROS_GOLD,
        "nos": [
            {
                "id": "src",
                "tipo": "sql",
                "sql": (
                    "SELECT n, CAST(:competencia_inicio AS VARCHAR) AS comp "
                    "FROM (VALUES (1), (2)) t(n)"
                ),
            },
            {
                "id": "out",
                "tipo": "saida",
                "config": {
                    "destino": "lake",
                    "camada": "gold",
                    "dataset": dataset,
                    "particao": _PARTICAO_GOLD,
                },
            },
        ],
        "arestas": [{"de": "src", "para": "out"}],
    }


async def test_particao_gold_por_competencia_le_a_corrida_da_competencia(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """Two runs of the SAME unit in different competences publish to two scopes. Reading the 1st with
    `competencia=2026-08` (month, or day 1) returns ITS data; asking the 1st for `2026-07` is asking
    the wrong run - a 404 naming what it published, never "zero rows" nor the 2nd run data."""
    lake = _lake(tmp_path, monkeypatch)
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="pub8@example.com", perfil="especialista"
        )
        pid = await _criar_publicado(
            cliente, h, _def_gold_competencia("pub_comp"), "Gold by competence"
        )
        e1 = await _executar(cliente, h, pid, {"operacao_id": "188", "competencia": "2026-08"})
        e2 = await _executar(cliente, h, pid, {"operacao_id": "188", "competencia": "2026-07"})
        for eid, dia in ((e1, "2026-08-01"), (e2, "2026-07-01")):
            corrida = (
                lake
                / "gold"
                / "dev"
                / "pub_comp"
                / "operacao_id=188"
                / f"competencia={dia}"
                / f"execucao_id={eid}"
            )
            assert (corrida / "_indice.json").is_file(), "precondition: index under ITS scope"

        for pedido in ("2026-08", "2026-08-01"):
            r = await cliente.get(
                _dados("pub_comp"),
                params={
                    "execucao_id": e1,
                    "operacao_id": "188",
                    "competencia": pedido,
                    "ordenar": "n",
                },
                headers=h,
            )
            assert r.status_code == 200, r.text
            corpo = r.json()
            assert corpo["execucao_id"] == e1
            assert corpo["total"] == 2
            assert [(li["n"], li["comp"]) for li in corpo["linhas"]] == [
                (1, "2026-08-01"),
                (2, "2026-08-01"),
            ]
            assert [p["particao"].get("competencia") for p in corpo["particoes"]] == ["2026-08-01"]

        errada = await cliente.get(
            _dados("pub_comp"), params={"execucao_id": e1, "competencia": "2026-07"}, headers=h
        )
        assert errada.status_code == 404, errada.text
        erro = errada.json()["erro"]
        assert erro["codigo"] == "publicado_ausente"
        assert "pub_comp" in erro["mensagem"]
        assert "competencia=2026-08-01" in erro["mensagem"]
        assert "competencia=2026-07" in erro["mensagem"]

        c2 = (
            await cliente.get(
                _dados("pub_comp"),
                params={"execucao_id": e2, "competencia": "2026-07"},
                headers=h,
            )
        ).json()
        assert {li["comp"] for li in c2["linhas"]} == {"2026-07-01"}
    finally:
        _limpar_settings()


async def test_filtro_por_bind_e_coluna_validada(owner_engine, cliente, tmp_path, monkeypatch):
    """(c) A hostile value returns 0 rows without error (it is compared, not executed); `~` is
    contains; a column the dataset does not have is a 400 NAMING the column."""
    _lake(tmp_path, monkeypatch)
    try:
        h, _, e1, _ = await _duas_corridas(owner_engine, cliente, "pub4@example.com", "pub_c")
        hostil = await cliente.get(
            _dados("pub_c"), params={"execucao_id": e1, "filtro": "lote=x' OR '1'='1"}, headers=h
        )
        assert hostil.status_code == 200, hostil.text
        assert hostil.json()["total"] == 0
        assert hostil.json()["linhas"] == []

        contem = await cliente.get(
            _dados("pub_c"), params={"execucao_id": e1, "filtro": ["lote~a", "valor=30"]}, headers=h
        )
        assert contem.json()["total"] == 1  # ILIKE: 'a' matches batch 'A'

        sem_coluna = await cliente.get(
            _dados("pub_c"), params={"execucao_id": e1, "filtro": "does_not_exist=1"}, headers=h
        )
        assert sem_coluna.status_code == 400
        assert sem_coluna.json()["erro"]["codigo"] == "coluna_inexistente"
        assert "does_not_exist" in sem_coluna.json()["erro"]["mensagem"]
    finally:
        _limpar_settings()


async def test_404_nomeado(owner_engine, cliente, tmp_path, monkeypatch):
    """(e) A nonexistent run, a dataset the run did not publish and a layer outside the list."""
    _lake(tmp_path, monkeypatch)
    try:
        h, _, e1, _ = await _duas_corridas(owner_engine, cliente, "pub5@example.com", "pub_e")
        inexistente = await cliente.get(
            _dados("pub_e"), params={"execucao_id": str(uuid.uuid4())}, headers=h
        )
        assert inexistente.status_code == 404
        assert inexistente.json()["erro"]["codigo"] == "nao_encontrado"

        outro = await cliente.get(_dados("other_dataset"), params={"execucao_id": e1}, headers=h)
        assert outro.status_code == 404
        assert outro.json()["erro"]["codigo"] == "publicado_ausente"
        assert "other_dataset" in outro.json()["erro"]["mensagem"]

        # Same dataset, wrong layer: the run published to gold, not to silver.
        silver = await cliente.get(_dados("pub_e", "silver"), params={"execucao_id": e1}, headers=h)
        assert silver.status_code == 404
        assert silver.json()["erro"]["codigo"] == "publicado_ausente"

        for camada in ("bronze", "landing", "quarantine", "relatorios"):
            r = await cliente.get(_dados("pub_e", camada), params={"execucao_id": e1}, headers=h)
            assert r.status_code == 404, camada
            assert camada in r.json()["erro"]["mensagem"]
    finally:
        _limpar_settings()


async def test_rbac_na_borda(owner_engine, cliente, tmp_path, monkeypatch):
    """(f) specialist 200, operator 403, anonymous 401; a `sistema` pipeline disappears for anyone
    who is not admin (generic 403) and stays readable by the admin."""
    _lake(tmp_path, monkeypatch)
    try:
        h, pid, e1, _ = await _duas_corridas(owner_engine, cliente, "pub6@example.com", "pub_r")
        params = {"execucao_id": e1}
        assert (await cliente.get(_dados("pub_r"), params=params, headers=h)).status_code == 200
        assert (await cliente.get(_dados("pub_r"), params=params)).status_code == 401

        _, h_op = await usuario_logado(
            owner_engine, cliente, email="pub6op@example.com", perfil="operador"
        )
        assert (await cliente.get(_dados("pub_r"), params=params, headers=h_op)).status_code == 403
        assert (
            await cliente.get(
                "/publicados/gold/pub_r/indice",
                params={"ambiente": "dev", **params},
                headers=h_op,
            )
        ).status_code == 403

        _, h_adm = await usuario_logado(
            owner_engine, cliente, email="pub6adm@example.com", perfil="admin"
        )
        marcou = await cliente.patch(
            f"/pipelines/{pid}/sistema", json={"sistema": True}, headers=h_adm
        )
        assert marcou.status_code == 200, marcou.text
        negado = await cliente.get(_dados("pub_r"), params=params, headers=h)
        assert negado.status_code == 403
        assert negado.json()["erro"]["codigo"] == "acesso_negado"
        assert (await cliente.get(_dados("pub_r"), params=params, headers=h_adm)).status_code == 200
    finally:
        _limpar_settings()


async def test_indice_da_corrida(owner_engine, cliente, tmp_path, monkeypatch):
    """(g) `/indice` returns the IMMUTABLE `_indice.json` of the requested run (with partition) - not
    `_atual.json`, which the 2nd run already merged - and the manifest when there is no partition."""
    _lake(tmp_path, monkeypatch)
    try:
        h, _, e1, e2 = await _duas_corridas(owner_engine, cliente, "pub7@example.com", "pub_g")
        r = await cliente.get(
            "/publicados/gold/pub_g/indice",
            params={"ambiente": "dev", "execucao_id": e1},
            headers=h,
        )
        assert r.status_code == 200, r.text
        indice = r.json()
        assert indice["execucao_id"] == e1
        assert indice["chaves"] == ["operacao_id", "project_id"]
        assert {p["particao"]["project_id"]: p["linhas"] for p in indice["particoes"]} == {
            "1201": 2,
            "1202": 1,
        }
        i2 = (
            await cliente.get(
                "/publicados/gold/pub_g/indice",
                params={"ambiente": "dev", "execucao_id": e2},
                headers=h,
            )
        ).json()
        assert [p["particao"]["project_id"] for p in i2["particoes"]] == ["1201"]

        pid = await _criar_publicado(cliente, h, _def_sem_particao("pub_g_man"), "Manifesto")
        eid = await _executar(cliente, h, pid)
        man = (
            await cliente.get(
                "/publicados/silver/pub_g_man/indice", params={"execucao_id": eid}, headers=h
            )
        ).json()
        assert man["execucao_id"] == eid
        assert man["linhas"] == 25
        assert man["caminho"].endswith("dados.parquet")
    finally:
        _limpar_settings()


# --- timestamptz on the route, index without partition, as-of that does not partition -----------


def _def_simples(dataset: str, sql: str, parametros: list | None = None) -> dict:
    definicao = {
        "nos": [
            {"id": "src", "tipo": "sql", "sql": sql},
            {
                "id": "out",
                "tipo": "saida",
                "config": {"destino": "lake", "camada": "silver", "dataset": dataset},
            },
        ],
        "arestas": [{"de": "src", "para": "out"}],
    }
    if parametros is not None:
        definicao["parametros"] = parametros
    return definicao


async def test_timestamptz_na_rota_sai_em_sao_paulo_e_o_valor_copiado_casa(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """A TIMESTAMPTZ column (the `timestamptz` of a Postgres connection, or `now()` in the node SQL)
    used to break GET .../dados with a 500 without the error envelope, with or without a filter."""
    _lake(tmp_path, monkeypatch)
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="pub9@example.com", perfil="especialista"
        )
        sql = (
            "SELECT n, CAST(r AS TIMESTAMPTZ) AS registrado FROM (VALUES "
            "(1, '2026-08-06 12:17:23-03'), (2, '2026-08-07 08:00:00-03')) t(n, r)"
        )
        pid = await _criar_publicado(cliente, h, _def_simples("pub_tz", sql), "With timezone")
        eid = await _executar(cliente, h, pid)
        r = await cliente.get(
            _dados("pub_tz", "silver"), params={"execucao_id": eid, "ordenar": "n"}, headers=h
        )
        assert r.status_code == 200, r.text
        linhas = r.json()["linhas"]
        assert [li["registrado"] for li in linhas] == [
            "2026-08-06T12:17:23-03:00",
            "2026-08-07T08:00:00-03:00",
        ]
        f = await cliente.get(
            _dados("pub_tz", "silver"),
            params={"execucao_id": eid, "filtro": f"registrado={linhas[0]['registrado']}"},
            headers=h,
        )
        assert f.status_code == 200, f.text
        assert [li["n"] for li in f.json()["linhas"]] == [1]
    finally:
        _limpar_settings()


async def test_indice_sem_particao_valida_pelas_colunas_do_indice_e_sem_elas_e_409(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """A run with NO partition at all (`particoes: []`, what column partitioning with
    `permitir_vazio` and zero rows produces): there is no parquet to read the schema from. With
    `colunas` in the index, the read checks filter/order/aggregate against them and returns the
    REQUESTED key; an old index without `colunas` is a named 409 - never a 200 with `colunas: []`
    accepting a column that does not exist.

    The index is put in the shape of the case directly in the lake: running `permitir_vazio` with
    zero rows brings the node down in `engine._publicar_saida` (`executemany` with an empty list)
    before writing the index - an engine defect, outside this read."""
    lake = _lake(tmp_path, monkeypatch)
    try:
        h, _, e1, _ = await _duas_corridas(owner_engine, cliente, "pub10@example.com", "pub_vz")
        arq = next((lake / "gold" / "dev" / "pub_vz").rglob(f"execucao_id={e1}/_indice.json"))
        indice = {**json.loads(arq.read_text()), "particoes": []}

        # The index in the contract shape (`publicar_indice` is what writes it).
        arq.write_text(json.dumps({**indice, "colunas": ["PROJECTID", "valor", "lote"]}))
        r = await cliente.get(
            _dados("pub_vz"),
            params={"execucao_id": e1, "agregar": "count_distinct:projectid", "filtro": "valor=1"},
            headers=h,
        )
        assert r.status_code == 200, r.text
        corpo = r.json()
        assert [c["nome"] for c in corpo["colunas"]] == ["PROJECTID", "valor", "lote"]
        assert corpo["total"] == 0 and corpo["linhas"] == []
        assert corpo["agregados"] == {"count_distinct": {"projectid": "0"}}
        errada = await cliente.get(
            _dados("pub_vz"),
            params={"execucao_id": e1, "filtro": "doesnotexist=1", "ordenar": "tb"},
            headers=h,
        )
        assert errada.status_code == 400, errada.text
        assert errada.json()["erro"]["codigo"] == "coluna_inexistente"

        arq.write_text(json.dumps({k: v for k, v in indice.items() if k != "colunas"}))
        antigo = await cliente.get(_dados("pub_vz"), params={"execucao_id": e1}, headers=h)
        assert antigo.status_code == 409, antigo.text
        assert antigo.json()["erro"]["codigo"] == "esquema_indisponivel"
        assert "pub_vz" in antigo.json()["erro"]["mensagem"]
    finally:
        _limpar_settings()


async def test_as_of_que_nao_particiona_e_conferido_pelo_instante(
    owner_engine, cliente, tmp_path, monkeypatch
):
    """The run executed with as-of 12:17:23; asking for 23:59:59 of the same day (or the day, which the
    resolver reads as end of day) is asking for ANOTHER run - 404, not this run data."""
    _lake(tmp_path, monkeypatch)
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="pub11@example.com", perfil="especialista"
        )
        definicao = _def_simples(
            "pub_asof",
            "SELECT CAST(:data_registro AS VARCHAR) AS dr",
            [{"nome": "data_registro", "tipo": "as_of", "obrigatorio": True}],
        )
        pid = await _criar_publicado(cliente, h, definicao, "As-of")
        eid = await _executar(cliente, h, pid, {"data_registro": "2026-08-06T12:17:23"})
        for pedido in ("2026-08-06T12:17:23", "2026-08-06 12:17:23"):
            r = await cliente.get(
                _dados("pub_asof", "silver"),
                params={"execucao_id": eid, "data_registro": pedido},
                headers=h,
            )
            assert r.status_code == 200, (pedido, r.text)
        for pedido in ("2026-08-06T23:59:59", "2026-08-06"):
            r = await cliente.get(
                _dados("pub_asof", "silver"),
                params={"execucao_id": eid, "data_registro": pedido},
                headers=h,
            )
            assert r.status_code == 404, (pedido, r.text)
            assert r.json()["erro"]["codigo"] == "publicado_ausente"
    finally:
        _limpar_settings()
