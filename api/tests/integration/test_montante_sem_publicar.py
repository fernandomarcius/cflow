"""`montante_publica: false` on the ROOT of a chained dispatch.

The upstream really runs - it computes and writes a snapshot to `execucao_nos`, which is what the
root reads through the freshness map - but it does not publish: no lake object, no pointer, no
index, no file in `export_dir`, no default report. The root publishes normally. Without the key,
everything is as before (degeneration). An effect node upstream with the flag on is refused at
dispatch, before any run exists.

Both paths: the workflow engine (in-process worker from the conftest, `ExecutarDAG` ->
`ExecutarPipeline` -> `executar_no_act`) and inline (`CFLOW_USAR_TEMPORAL=false`,
`engine.executar_encadeado`).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao, contrato_ambiente_gold

pytestmark = pytest.mark.integration

DS_W = "load_mont_w"
DS_RAIZ = "raiz_mont"
PARAMS = {"ambiente": "dev", "operacao_id": "61"}


@pytest.fixture
def lake(tmp_path, monkeypatch):
    """Test lake under `tmp`. The settings are cached: without clearing, `CFLOW_LAKE_DIR` has no effect."""
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    yield tmp_path / "lake"
    get_settings.cache_clear()


@pytest.fixture
def relatorios_padrao(monkeypatch):
    """Records who reached the EARLY report generation (`engine.finalizar`). The default report is
    also an object in the lake: an upstream without publishing cannot get there."""
    from cflow_core.relatorios import service

    chamadas: list[str] = []

    async def _registrar(execucao_id) -> dict:
        chamadas.append(str(execucao_id))
        return {}

    monkeypatch.setattr(service, "enfileirar_padroes", _registrar)
    return chamadas


def _modo(modo: str, monkeypatch) -> None:
    if modo == "inline":
        from cflow_core.config import get_settings

        monkeypatch.setattr(get_settings(), "usar_temporal", False)


async def _publicar(cliente, h, nome: str, definicao: dict) -> str:
    definicao = contrato_ambiente_gold(definicao, declarar=True)
    pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 200, r.text
    r = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    assert r.status_code in (200, 201), r.text
    return pid


def _def_w(export_dir: str) -> dict:
    """The upstream: a gold output PARTITIONED by parameter (the load template, `operacao_id=X/
    _atual.json`) and a file output (the `export_dir`)."""
    return {
        "parametros": [{"nome": "operacao_id", "tipo": "texto", "obrigatorio": True}],
        "nos": [
            {"id": "src", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1,'a'),(2,'b')) t(n,c)"},
            {
                "id": "out",
                "tipo": "saida",
                "config": {
                    "destino": "lake",
                    "camada": "gold",
                    "dataset": DS_W,
                    "particao": [{"origem": "parametro", "nome": "operacao_id"}],
                },
            },
            {"id": "arq", "tipo": "saida", "config": {"nome": "w_file", "caminho": export_dir}},
        ],
        "arestas": [{"de": "src", "para": "out"}, {"de": "src", "para": "arq"}],
    }


def _def_raiz(w_id: str, **marca) -> dict:
    return {
        **marca,
        "parametros": [{"nome": "operacao_id", "tipo": "texto", "obrigatorio": True}],
        "nos": [
            {
                "id": "src",
                "tipo": "fonte",
                "config": {"origem": "pipeline", "pipeline_id": w_id, "no_id": "out"},
            },
            {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            {
                "id": "pub",
                "tipo": "saida",
                "config": {"destino": "lake", "camada": "gold", "dataset": DS_RAIZ},
            },
        ],
        "arestas": [{"de": "src", "para": "cont"}, {"de": "cont", "para": "pub"}],
    }


async def _disparar(cliente, h, raiz_id: str, modo: str) -> tuple[dict, str]:
    """Dispatch the chain and return (root detail, upstream execucao_id), already settled."""
    r = await cliente.post(
        f"/pipelines/{raiz_id}/executar-encadeado", json={"parametros": PARAMS}, headers=h
    )
    assert r.status_code == 200, r.text
    res = r.json()
    w_exec = res["cadeia"][0]["execucao_id"]
    if modo == "temporal":
        w_det = await aguardar_execucao(cliente, w_exec, h)
        raiz_det = await aguardar_execucao(cliente, res["id"], h)
    else:
        w_det = (await cliente.get(f"/execucoes/{w_exec}", headers=h)).json()
        raiz_det = (await cliente.get(f"/execucoes/{res['id']}", headers=h)).json()
    assert w_det["status"] == "concluida", w_det
    return raiz_det, w_exec


async def _nos_da_execucao(owner_engine, execucao_id: str) -> dict[str, dict]:
    async with owner_engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT no_id, status, snapshot, linhas FROM execucao_nos "
                        "WHERE execucao_id = CAST(:e AS uuid)"
                    ),
                    {"e": execucao_id},
                )
            )
            .mappings()
            .all()
        )
    return {r["no_id"]: dict(r) for r in rows}


@pytest.mark.parametrize("modo", ["temporal", "inline"])
async def test_montante_calcula_mas_nao_publica(
    owner_engine, cliente, lake, tmp_path, monkeypatch, relatorios_padrao, modo
):
    _modo(modo, monkeypatch)
    _, h = await usuario_logado(
        owner_engine, cliente, email=f"mont1{modo}@example.com", perfil="especialista"
    )
    export = tmp_path / "export"
    w_id = await _publicar(cliente, h, "Upstream-mont", _def_w(str(export)))
    raiz_id = await _publicar(cliente, h, "root-mont", _def_raiz(w_id, montante_publica=False))

    raiz_det, w_exec = await _disparar(cliente, h, raiz_id, modo)

    assert raiz_det["status"] == "concluida", raiz_det
    # The root read the upstream snapshot of THIS run (2 rows), and published its own.
    assert {n["no_id"]: n["linhas"] for n in raiz_det["nos"]}["src"] == 2
    assert (lake / "gold" / "dev" / DS_RAIZ / "_atual.json").is_file()
    # The upstream computed and wrote a snapshot on all three nodes - including the output nodes,
    # which is what the root reads through the `origem: "pipeline"` source.
    nos_w = await _nos_da_execucao(owner_engine, w_exec)
    assert {k: v["status"] for k, v in nos_w.items()} == {
        "src": "concluida",
        "out": "concluida",
        "arq": "concluida",
    }
    assert nos_w["out"]["linhas"] == 2 and nos_w["out"]["snapshot"]
    # ... and it published NOTHING: neither the gold dataset (object, scope pointer, index), nor the
    # export_dir file, nor the default report.
    assert not (lake / "gold" / "dev" / DS_W).exists()
    assert sorted(p.name for p in (lake / "gold" / "dev").iterdir()) == [DS_RAIZ]
    assert not export.exists() or not any(export.iterdir())
    assert w_exec not in relatorios_padrao
    assert raiz_det["id"] in relatorios_padrao


@pytest.mark.parametrize("modo", ["temporal", "inline"])
async def test_sem_a_chave_a_montante_publica_como_hoje(
    owner_engine, cliente, lake, tmp_path, monkeypatch, relatorios_padrao, modo
):
    """Degeneration: every root that exists today does not declare the key, and none may change."""
    _modo(modo, monkeypatch)
    _, h = await usuario_logado(
        owner_engine, cliente, email=f"mont2{modo}@example.com", perfil="especialista"
    )
    export = tmp_path / "export"
    w_id = await _publicar(cliente, h, "Upstream-mont", _def_w(str(export)))
    raiz_id = await _publicar(cliente, h, "root-mont", _def_raiz(w_id))

    raiz_det, w_exec = await _disparar(cliente, h, raiz_id, modo)

    assert raiz_det["status"] == "concluida", raiz_det
    assert (lake / "gold" / "dev" / DS_W / "operacao_id=61" / "_atual.json").is_file()
    assert (lake / "gold" / "dev" / DS_W / "_atual.json").is_file()  # run index
    assert (export / "w_file.parquet").is_file()
    assert (lake / "gold" / "dev" / DS_RAIZ / "_atual.json").is_file()
    assert w_exec in relatorios_padrao


def _cfg_owner(owner_engine) -> tuple[dict, str]:
    u = owner_engine.url
    return (
        {"host": u.host, "porta": int(u.port), "base": u.database, "usuario": u.username},
        u.password,
    )


async def _conexao_escrita(owner_engine, cliente, h) -> str:
    config, senha = _cfg_owner(owner_engine)
    cid = (
        await cliente.post(
            "/conexoes",
            json={"nome": "write-mont", "tipo": "postgres", "config": config, "segredo": senha},
            headers=h,
        )
    ).json()["id"]
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE conexoes SET permitir_escrita = true WHERE id = CAST(:i AS uuid)"),
            {"i": cid},
        )
    return cid


@pytest.mark.parametrize("modo", ["temporal", "inline"])
async def test_efeito_a_montante_com_a_marca_e_recusado_antes_de_criar(
    owner_engine, cliente, lake, monkeypatch, modo
):
    """Running `escrita_sql` in a run that claims to have no effect would be lying. The refusal is at
    DISPATCH, before pre-creation: no run may be left 'executando' with nobody to close it."""
    _modo(modo, monkeypatch)
    _, h = await usuario_logado(
        owner_engine, cliente, email=f"mont3{modo}@example.com", perfil="especialista"
    )
    cid = await _conexao_escrita(owner_engine, cliente, h)
    w_id = await _publicar(
        cliente,
        h,
        "Upstream-with-effect",
        {
            "nos": [
                {"id": "src", "tipo": "sql", "sql": "SELECT 1 AS n"},
                {
                    "id": "write_target",
                    "tipo": "escrita_sql",
                    "config": {
                        "conexao_id": cid,
                        "modo": "tabela",
                        "schema": "write_test",
                        "tabela": "target",
                        "estrategia": "append",
                    },
                },
            ],
            "arestas": [{"de": "src", "para": "write_target"}],
        },
    )
    raiz_id = await _publicar(
        cliente,
        h,
        "root-effect",
        {
            "montante_publica": False,
            "nos": [
                {
                    "id": "src",
                    "tipo": "fonte",
                    "config": {"origem": "pipeline", "pipeline_id": w_id, "no_id": "src"},
                },
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )

    r = await cliente.post(f"/pipelines/{raiz_id}/executar-encadeado", headers=h)

    assert r.status_code == 400, r.text
    erro = r.json()["erro"]
    assert erro["codigo"] == "montante_com_efeito"
    assert "Upstream-with-effect" in erro["mensagem"] and "write_target" in erro["mensagem"]
    async with owner_engine.connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM execucoes"))).scalar_one()
    assert n == 0


async def test_disparo_agendado_marca_so_a_montante(owner_engine, cliente, lake, relatorios_padrao):
    """The SCHEDULER dispatch (`disparo.disparar`, the same as "Run now") hands `ExecutarDAG` the flag
    per item: the upstream carries `sem_publicar`, the root does not."""
    from cflow_core.execucao import disparo

    ator, h = await usuario_logado(
        owner_engine, cliente, email="mont4@example.com", perfil="especialista"
    )
    w_id = await _publicar(cliente, h, "Upstream-mont", _def_w("/must/not/be/used"))
    raiz_id = await _publicar(cliente, h, "root-mont", _def_raiz(w_id, montante_publica=False))
    enviados: list[tuple[str, list]] = []

    async def _registrar(nome: str, args: list, workflow_id: str) -> None:
        enviados.append((nome, args))

    raiz_exec = await disparo.disparar(
        pipeline_id=uuid.UUID(raiz_id),
        ator_id=ator,
        parametros=PARAMS,
        iniciar_workflow=_registrar,
    )
    try:
        [(nome, args)] = enviados
        assert nome == "ExecutarDAG"
        cadeia = {c["pipeline_id"]: c for c in args[0]}
        assert cadeia[w_id].get("sem_publicar") is True
        assert "sem_publicar" not in cadeia[raiz_id]
        assert cadeia[raiz_id]["execucao_id"] == raiz_exec
    finally:
        # Only recorded: no workflow will run these pre-created runs.
        await disparo.abortar(
            [c["execucao_id"] for _, a in enviados for c in a[0]], "test: only recorded."
        )
