"""Execution of the published graph: reads the COMPLETE source, Parquet snapshot + per-node trail."""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao

pytestmark = pytest.mark.integration


async def test_executar_le_completo_e_gera_snapshot(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="exe@example.com", perfil="especialista"
    )

    # file source with 3 rows
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        f.write("valor\n10\n20\n30\n")
        caminho = f.name
    cid = (
        await cliente.post(
            "/conexoes",
            json={"nome": "arq-exec", "tipo": "arquivo", "config": {"caminho": caminho}},
            headers=h,
        )
    ).json()["id"]

    pid = (await cliente.post("/pipelines", json={"nome": "exec-demo"}, headers=h)).json()["id"]
    # amostra=1 in preview; the EXECUTION ignores it and reads everything (3 rows)
    definicao = {
        "nos": [
            {"id": "n1", "tipo": "fonte", "config": {"conexao_id": cid, "sql": "", "amostra": 1}},
            {
                "id": "f",
                "tipo": "guiada",
                "config": {"op": {"tipo": "filtrar", "condicao": "CAST(valor AS INTEGER) > 15"}},
            },
        ],
        "arestas": [{"de": "n1", "para": "f"}],
    }
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )

    r = await cliente.post(f"/pipelines/{pid}/executar", headers=h)
    assert r.status_code == 200
    res = await aguardar_execucao(
        cliente, r.json()["id"], h
    )  # Temporal workflow: 1 activity per node
    assert res["status"] == "concluida"
    nos = {n["no_id"]: n for n in res["nos"]}
    assert nos["n1"]["linhas"] == 3  # read the COMPLETE source (not the 1-row sample)
    assert nos["f"]["linhas"] == 2  # filtered > 15 -> 20, 30
    assert nos["n1"]["hash"] and nos["f"]["hash"]
    assert os.path.isfile(nos["n1"]["snapshot"]) and os.path.isfile(nos["f"]["snapshot"])

    # queryable execution trail
    det = (await cliente.get(f"/execucoes/{res['id']}", headers=h)).json()
    assert det["status"] == "concluida" and len(det["nos"]) == 2
    # The detail carries timestamps and trigger, so the UI shows start/duration/origin in one call.
    assert det["iniciada_em"] and det["concluida_em"]
    assert det["origem"] == "manual" and det["agendamento_id"] is None
    assert det["parametros"] == {} and det["versao"] == res["versao"]


async def test_reuso_incremental(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="exr@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "reuso"}, headers=h)).json()["id"]
    definicao = {
        "nos": [
            {"id": "n1", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1),(2),(3)) t(n)"},
            {
                "id": "f",
                "tipo": "guiada",
                "config": {"op": {"tipo": "filtrar", "condicao": "n > 1"}},
            },
        ],
        "arestas": [{"de": "n1", "para": "f"}],
    }
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )

    r1 = await aguardar_execucao(
        cliente, (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()["id"], h
    )
    assert all(n["reaproveitado"] is False for n in r1["nos"])  # 1st run: everything computed

    r2 = await aguardar_execucao(
        cliente, (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()["id"], h
    )
    assert all(
        n["reaproveitado"] is True for n in r2["nos"]
    )  # 2nd: same SQL/op + same data -> reused
    assert {n["no_id"]: n["linhas"] for n in r2["nos"]} == {"n1": 3, "f": 2}  # identical result


async def test_reuso_fonte_entre_pipelines_da_cadeia(owner_engine, app_engine, cliente):
    """Pipeline DAG: a source (same connection + SQL) is reused across pipelines of the SAME chain.
    A and C (which depends on A) have the SAME source node; when C runs, A has finished, so the
    cache is GUARANTEED. (Between SIBLINGS running at the same time, the single flight makes the
    second wait for the first; see `test_irmas_simultaneas_extraem_uma_vez_so`.) A NEW chain (new
    dispatch) does NOT reuse the previous one: the source stays fresh outside the chain."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="cad@example.com", perfil="especialista"
    )

    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        f.write("valor\n1\n2\n")
        caminho = f.name
    cid = (
        await cliente.post(
            "/conexoes",
            json={"nome": "arq-cadeia", "tipo": "arquivo", "config": {"caminho": caminho}},
            headers=h,
        )
    ).json()["id"]
    def_fonte = {
        "nos": [
            {"id": "n1", "tipo": "fonte", "config": {"conexao_id": cid, "sql": "", "amostra": 1}}
        ],
        "arestas": [],
    }

    async def _publicar(nome: str, definicao: dict) -> str:
        pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
        await cliente.put(
            f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
        )
        await cliente.post(
            f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
        )
        return pid

    pid_a = await _publicar("cadeia-a", def_fonte)  # level 0
    def_c = {
        # `dep_a` pushes C to level 1 (depends on A); `n1` is the SAME connection source A uses;
        # when it runs, level 0 (A) has finished, so there is no race with A.
        "nos": [
            {
                "id": "dep_a",
                "tipo": "fonte",
                "config": {"origem": "pipeline", "pipeline_id": pid_a, "no_id": "n1"},
            },
            {"id": "n1", "tipo": "fonte", "config": {"conexao_id": cid, "sql": "", "amostra": 1}},
            {
                "id": "resumo_c",
                "tipo": "guiada",
                "config": {"op": {"tipo": "juntar", "modo": "uniao"}},
            },
        ],
        "arestas": [{"de": "dep_a", "para": "resumo_c"}, {"de": "n1", "para": "resumo_c"}],
    }
    pid_c = await _publicar("cadeia-c", def_c)  # level 1
    raiz_def = {
        "nos": [
            {
                "id": "fa",
                "tipo": "fonte",
                "config": {"origem": "pipeline", "pipeline_id": pid_a, "no_id": "n1"},
            },
            {
                "id": "fc",
                "tipo": "fonte",
                "config": {"origem": "pipeline", "pipeline_id": pid_c, "no_id": "resumo_c"},
            },
            {
                "id": "resumo",
                "tipo": "guiada",
                "config": {"op": {"tipo": "juntar", "modo": "uniao"}},
            },
        ],
        "arestas": [{"de": "fa", "para": "resumo"}, {"de": "fc", "para": "resumo"}],
    }
    pid_raiz = await _publicar("cadeia-raiz", raiz_def)  # level 2

    async def _disparar_e_ler_ac() -> tuple[dict, dict]:
        disparo = (await cliente.post(f"/pipelines/{pid_raiz}/executar", headers=h)).json()
        cadeia_map = {c["pipeline_id"]: c["execucao_id"] for c in disparo["cadeia"]}
        res = await aguardar_execucao(cliente, disparo["id"], h)
        assert res["status"] == "concluida"
        det_a = (await cliente.get(f"/execucoes/{cadeia_map[pid_a]}", headers=h)).json()
        det_c = (await cliente.get(f"/execucoes/{cadeia_map[pid_c]}", headers=h)).json()
        no_a = next(n for n in det_a["nos"] if n["no_id"] == "n1")
        no_c = next(n for n in det_c["nos"] if n["no_id"] == "n1")
        return no_a, no_c

    no_a, no_c = await _disparar_e_ler_ac()
    assert no_a["reaproveitado"] is False  # level 0, first in the chain: really extracts
    assert no_c["reaproveitado"] is True  # level 1, runs AFTER A finishes: cache guaranteed
    assert no_a["linhas"] == no_c["linhas"] == 2

    # Negative: NEW dispatch (new chain): the source re-runs in A, with no cross-chain cache.
    no_a2, no_c2 = await _disparar_e_ler_ac()
    assert no_a2["reaproveitado"] is False
    assert no_c2["reaproveitado"] is True  # inside the NEW chain, dedup across levels still applies


async def test_listar_execucoes(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="exl@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "lst"}, headers=h)).json()["id"]
    definicao = {"nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}], "arestas": []}
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    eid = (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()["id"]
    await aguardar_execucao(cliente, eid, h)
    lst = await cliente.get(f"/pipelines/{pid}/execucoes", headers=h)
    assert lst.status_code == 200
    corpo = lst.json()
    assert (
        corpo["total"] == 1
        and len(corpo["itens"]) == 1
        and corpo["itens"][0]["status"] == "concluida"
    )


async def test_executar_sem_definicao_400(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="exe2@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "vazio"}, headers=h)).json()["id"]
    r = await cliente.post(f"/pipelines/{pid}/executar", headers=h)
    assert r.status_code == 400 and r.json()["erro"]["codigo"] == "sem_definicao"


async def _pipeline_simples(cliente, h, nome: str) -> str:
    """Create + publish a 1-source-node (CSV) pipeline and return its id."""
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        f.write("valor\n1\n2\n")
        caminho = f.name
    cid = (
        await cliente.post(
            "/conexoes",
            json={"nome": f"arq-{nome}", "tipo": "arquivo", "config": {"caminho": caminho}},
            headers=h,
        )
    ).json()["id"]
    pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
    definicao = {
        "nos": [
            {"id": "n1", "tipo": "fonte", "config": {"conexao_id": cid, "sql": "", "amostra": 1}}
        ],
        "arestas": [],
    }
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    return pid


async def test_executar_registra_pagina_e_usuario(owner_engine, app_engine, cliente):
    # The run records WHICH page the operator triggered it from + WHO triggered it; list/detail expose both.
    _, h = await usuario_logado(
        owner_engine, cliente, email="exp@example.com", perfil="especialista"
    )
    pid = await _pipeline_simples(cliente, h, "exec-pag")
    pag = (await cliente.post("/paginas", json={"nome": "Painel Origem"}, headers=h)).json()["id"]
    # `_autorizar_execucao` only allows pagina_id for a PUBLISHED page whose `execucao.pipeline_id`
    # references this pipeline; the block's view_id is not checked against the `views` table
    # (structure, not existence).
    paginadef = {
        "blocos": [{"tipo": "pivot", "view_id": str(uuid.uuid4()), "titulo": "x"}],
        "filtros_pagina": [],
        "execucao": {"pipeline_id": pid},
    }
    await cliente.put(
        f"/paginas/{pag}/rascunho", json={"definicao": paginadef, "base_numero": 0}, headers=h
    )
    await cliente.post(f"/paginas/{pag}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h)

    r = await cliente.post(f"/pipelines/{pid}/executar", json={"pagina_id": pag}, headers=h)
    assert r.status_code == 200
    eid = r.json()["id"]
    await aguardar_execucao(cliente, eid, h)

    lst = (await cliente.get(f"/pipelines/{pid}/execucoes", headers=h)).json()["itens"]
    assert lst[0]["pagina_id"] == pag
    assert lst[0]["pagina_nome"] == "Painel Origem"
    assert lst[0]["ator_nome"] == "Teste"

    det = (await cliente.get(f"/execucoes/{eid}", headers=h)).json()
    assert det["pagina_nome"] == "Painel Origem"
    assert det["ator_nome"] == "Teste"


async def test_executar_sem_pagina_fica_nulo(owner_engine, app_engine, cliente):
    # Triggered without a page: pagina_id/pagina_nome are null, but the user is always recorded.
    _, h = await usuario_logado(
        owner_engine, cliente, email="exnp@example.com", perfil="especialista"
    )
    pid = await _pipeline_simples(cliente, h, "exec-np")
    eid = (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()["id"]
    await aguardar_execucao(cliente, eid, h)
    lst = (await cliente.get(f"/pipelines/{pid}/execucoes", headers=h)).json()["itens"]
    assert lst[0]["pagina_id"] is None
    assert lst[0]["pagina_nome"] is None
    assert lst[0]["ator_nome"] == "Teste"


async def test_irmas_simultaneas_extraem_uma_vez_so(owner_engine, app_engine, cliente, monkeypatch):
    """Single flight of the extraction within a chain. A and B are siblings (neither reads the
    other) with the SAME connection source, and run at the same time. Previously both extracted:
    the cache only sees FINISHED snapshots, so the same slow source was read twice side by side.
    Now the second waits for the first and is served from the cache, with the SAME content."""
    import asyncio

    from cflow_core.conexoes import service as conexoes_service

    _, h = await usuario_logado(
        owner_engine, cliente, email="voo@example.com", perfil="especialista"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        f.write("valor\n1\n2\n3\n")
        caminho = f.name
    cid = (
        await cliente.post(
            "/conexoes",
            json={"nome": "arq-voo", "tipo": "arquivo", "config": {"caminho": caminho}},
            headers=h,
        )
    ).json()["id"]

    extracoes: list[str] = []
    original = conexoes_service.amostra

    async def _amostra_lenta(*args, **kwargs):
        # Hold the extraction long enough for the sibling to arrive while it is in flight.
        extracoes.append(str(args[0]))
        await asyncio.sleep(0.5)
        return await original(*args, **kwargs)

    monkeypatch.setattr(conexoes_service, "amostra", _amostra_lenta)

    async def _publicar(nome: str, definicao: dict) -> str:
        pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
        await cliente.put(
            f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
        )
        await cliente.post(
            f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
        )
        return pid

    def_fonte = {
        "nos": [{"id": "n1", "tipo": "fonte", "config": {"conexao_id": cid, "sql": ""}}],
        "arestas": [],
    }
    pid_a = await _publicar("voo-a", def_fonte)
    pid_b = await _publicar("voo-b", def_fonte)
    pid_raiz = await _publicar(
        "voo-raiz",
        {
            "nos": [
                {
                    "id": "fa",
                    "tipo": "fonte",
                    "config": {"origem": "pipeline", "pipeline_id": pid_a, "no_id": "n1"},
                },
                {
                    "id": "fb",
                    "tipo": "fonte",
                    "config": {"origem": "pipeline", "pipeline_id": pid_b, "no_id": "n1"},
                },
                {
                    "id": "u",
                    "tipo": "guiada",
                    "config": {"op": {"tipo": "juntar", "modo": "uniao"}},
                },
            ],
            "arestas": [{"de": "fa", "para": "u"}, {"de": "fb", "para": "u"}],
        },
    )
    disparo = (await cliente.post(f"/pipelines/{pid_raiz}/executar", headers=h)).json()
    res = await aguardar_execucao(cliente, disparo["id"], h)
    assert res["status"] == "concluida"
    cadeia = {c["pipeline_id"]: c["execucao_id"] for c in disparo["cadeia"]}
    nos = []
    for pid in (pid_a, pid_b):
        det = (await cliente.get(f"/execucoes/{cadeia[pid]}", headers=h)).json()
        nos.append(next(n for n in det["nos"] if n["no_id"] == "n1"))

    assert len(extracoes) == 1, f"the same extraction ran {len(extracoes)} times in the chain"
    assert sorted(n["reaproveitado"] for n in nos) == [False, True]
    assert nos[0]["linhas"] == nos[1]["linhas"] == 3
    assert nos[0]["hash"] == nos[1]["hash"]
    assert {n["no_id"]: n["linhas"] for n in res["nos"]}["u"] == 6
