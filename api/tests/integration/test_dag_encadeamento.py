"""A->B chaining (DAG of pipelines): chained execution runs the upstreams (`pipeline`-type
sources) in topological order and the root reads the FRESH snapshots just produced.

Covers: direct dependency (B runs even if A was never run manually), transitive (C->B->A in the
right order), cycle (A<->B -> 400 ciclo_pipelines) and denied authorization.
"""

from __future__ import annotations

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao


async def _criar(cliente, h, nome: str) -> str:
    return (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]


async def _publicar(cliente, h, pid: str, definicao: dict) -> None:
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )


def _fonte_de(a_id: str, no_id: str = "saida") -> dict:
    return {
        "id": "src",
        "tipo": "fonte",
        "config": {"origem": "pipeline", "pipeline_id": a_id, "no_id": no_id},
    }


async def test_encadeado_roda_dependencias(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="dag1@example.com", perfil="especialista"
    )
    # A: 3 rows in the "saida" node. NEVER run manually.
    a_id = await _criar(cliente, h, "A")
    await _publicar(
        cliente,
        h,
        a_id,
        {
            "nos": [
                {"id": "saida", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1),(2),(3)) t(n)"}
            ],
            "arestas": [],
        },
    )
    # B: pipeline-type source on A -> counts.
    b_id = await _criar(cliente, h, "B")
    await _publicar(
        cliente,
        h,
        b_id,
        {
            "nos": [
                _fonte_de(a_id),
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )
    # Chained: runs A (dependency) and then B, even though A was never run before.
    r = await cliente.post(f"/pipelines/{b_id}/executar-encadeado", headers=h)
    assert r.status_code == 200, r.text
    res = r.json()
    # chain = [A, B] (dependency first, root last).
    cadeia = res["cadeia"]
    assert [c["pipeline_id"] for c in cadeia] == [a_id, b_id]
    det = await aguardar_execucao(cliente, res["id"], h)
    assert det["status"] == "concluida"
    linhas = {n["no_id"]: n["linhas"] for n in det["nos"]}
    assert linhas["src"] == 3  # read A's FRESH snapshot, produced just now


async def test_encadeia_transitivo(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="dag2@example.com", perfil="especialista"
    )
    a_id = await _criar(cliente, h, "A-t")
    await _publicar(
        cliente,
        h,
        a_id,
        {"nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT 1 AS n"}], "arestas": []},
    )
    b_id = await _criar(cliente, h, "B-t")
    await _publicar(
        cliente,
        h,
        b_id,
        {
            "nos": [_fonte_de(a_id), {"id": "saida", "tipo": "sql", "sql": "SELECT * FROM src"}],
            "arestas": [{"de": "src", "para": "saida"}],
        },
    )
    c_id = await _criar(cliente, h, "C-t")
    await _publicar(
        cliente,
        h,
        c_id,
        {
            "nos": [
                _fonte_de(b_id),
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )
    r = await cliente.post(f"/pipelines/{c_id}/executar-encadeado", headers=h)
    assert r.status_code == 200, r.text
    res = r.json()
    cadeia = res["cadeia"]
    # topological order: A before B before C (root last).
    assert [c["pipeline_id"] for c in cadeia] == [a_id, b_id, c_id]
    # every execution in the chain completes (root read at the end).
    for c in cadeia:
        det = await aguardar_execucao(cliente, c["execucao_id"], h)
        assert det["status"] == "concluida"


async def test_ciclo_detectado(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="dag3@example.com", perfil="especialista"
    )
    a_id = await _criar(cliente, h, "A-ciclo")
    b_id = await _criar(cliente, h, "B-ciclo")
    # A depends on B and B depends on A (cycle). Publishes both.
    await _publicar(
        cliente,
        h,
        a_id,
        {
            "nos": [_fonte_de(b_id), {"id": "saida", "tipo": "sql", "sql": "SELECT * FROM src"}],
            "arestas": [{"de": "src", "para": "saida"}],
        },
    )
    await _publicar(
        cliente,
        h,
        b_id,
        {
            "nos": [_fonte_de(a_id), {"id": "saida", "tipo": "sql", "sql": "SELECT * FROM src"}],
            "arestas": [{"de": "src", "para": "saida"}],
        },
    )
    r = await cliente.post(f"/pipelines/{a_id}/executar-encadeado", headers=h)
    assert r.status_code == 400
    assert r.json()["erro"]["codigo"] == "ciclo_pipelines"


async def test_encadeado_paraleliza_irmas_sem_dependencia(owner_engine, cliente):
    """A and B are SIBLINGS (the root depends on both; neither depends on the other): both at
    level 0. ExecutarDAG runs the whole of level 0 in parallel (asyncio.gather) and only then runs
    the root. Correctness (not timing, which is flaky): the response keeps the usual [A, B, root]
    order, since parallelism is only in SCHEDULING, not in the contract; and the root only has the
    right row total if it waited for BOTH to finish before the union (proof that the level is
    respected, not just labelled).
    """
    _, h = await usuario_logado(
        owner_engine, cliente, email="dag5@example.com", perfil="especialista"
    )
    a_id = await _criar(cliente, h, "A-par")
    await _publicar(
        cliente,
        h,
        a_id,
        {
            "nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1),(2)) t(n)"}],
            "arestas": [],
        },
    )
    b_id = await _criar(cliente, h, "B-par")
    await _publicar(
        cliente,
        h,
        b_id,
        {
            "nos": [
                {"id": "saida", "tipo": "sql", "sql": "SELECT * FROM (VALUES (10),(20),(30)) t(n)"}
            ],
            "arestas": [],
        },
    )
    raiz_id = await _criar(cliente, h, "raiz-par")
    await _publicar(
        cliente,
        h,
        raiz_id,
        {
            "nos": [
                _fonte_de(a_id),
                {**_fonte_de(b_id), "id": "src_b"},
                {
                    "id": "uniao",
                    "tipo": "guiada",
                    "config": {"op": {"tipo": "juntar", "modo": "uniao"}},
                },
            ],
            "arestas": [{"de": "src", "para": "uniao"}, {"de": "src_b", "para": "uniao"}],
        },
    )
    r = await cliente.post(f"/pipelines/{raiz_id}/executar-encadeado", headers=h)
    assert r.status_code == 200, r.text
    res = r.json()
    # response contract intact: A and B (level 0, discovery order) before the root (level 1).
    assert [c["pipeline_id"] for c in res["cadeia"]] == [a_id, b_id, raiz_id]
    det = await aguardar_execucao(cliente, res["id"], h)
    assert det["status"] == "concluida"
    linhas = {n["no_id"]: n["linhas"] for n in det["nos"]}
    assert linhas["src"] == 2 and linhas["src_b"] == 3
    assert (
        linhas["uniao"] == 5
    )  # only matches if the root waited for A AND B to finish (level respected)


async def test_encadeado_exige_permissao(owner_engine, cliente):
    import uuid

    _, h = await usuario_logado(owner_engine, cliente, email="dag4@example.com", perfil="operador")
    # operator (without the specialist/admin profile) is blocked at the edge -> 403.
    r = await cliente.post(f"/pipelines/{uuid.uuid4()}/executar-encadeado", headers=h)
    assert r.status_code == 403


async def test_montante_de_cadeia_nao_leva_chave_e_a_raiz_em_voo_recusa(
    owner_engine, cliente, monkeypatch
):
    """Only the ROOT of a firing has a concurrency key. Two chains of the same operation needing
    the same upstream (a daily load) keep recomputing in parallel; blocking the upstream would
    abort the whole chain. A root already in flight, however, is refused, before any dependency
    is created."""
    import uuid

    from sqlalchemy import text

    from cflow_core.execucao import engine
    from cflow_core.execucao import reconciliacao as rec

    uid, h = await usuario_logado(
        owner_engine, cliente, email="dag-conc@example.com", perfil="especialista"
    )
    a_id = await _criar(cliente, h, "A-conc")
    await _publicar(
        cliente,
        h,
        a_id,
        {"nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT 1 AS n"}], "arestas": []},
    )
    b_id = await _criar(cliente, h, "B-conc")
    await _publicar(
        cliente,
        h,
        b_id,
        {
            "nos": [
                _fonte_de(a_id),
                {"id": "c", "tipo": "sql", "sql": "SELECT count(*) c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "c"}],
        },
    )
    # A in flight as the ROOT of another firing (with a key), "alive" in Temporal.
    a_voo = (await engine.criar_execucao(pipeline_id=uuid.UUID(a_id), ator_id=uid))["execucao_id"]

    async def _conectar(_host):
        return object()

    monkeypatch.setattr(rec, "conectar", _conectar)

    async def _viva(_c, _e, **_kw):
        return rec.Veredito(viva=True, desconhecida=False)

    monkeypatch.setattr(rec, "consultar_uma", _viva)

    # B (root) fires the chain: A runs as a DEPENDENCY even with A in flight in another run.
    r = await cliente.post(f"/pipelines/{b_id}/executar", headers=h)
    assert r.status_code == 200, r.text
    res = await aguardar_execucao(cliente, r.json()["id"], h)
    assert res["status"] == "concluida"
    async with owner_engine.connect() as conn:
        chaves = {
            str(row[0]): row[1]
            for row in (
                await conn.execute(
                    text(
                        "SELECT pipeline_id, chave_concorrencia FROM execucoes "
                        "WHERE cadeia_id = (SELECT cadeia_id FROM execucoes WHERE id = :e)"
                    ),
                    {"e": uuid.UUID(res["id"])},
                )
            ).all()
        }
    assert chaves[a_id] is None  # dependency: no key
    assert chaves[b_id]  # root: has a key
    assert (await cliente.get(f"/execucoes/{a_voo}", headers=h)).json()[
        "status"
    ] == "executando"  # the neighbouring run was not touched

    # Now the ROOT B is in flight (the earlier one already finished). Seeds an in-flight B and
    # fires again: 409 before creating any row of the chain.
    b_voo = (await engine.criar_execucao(pipeline_id=uuid.UUID(b_id), ator_id=uid))["execucao_id"]
    async with owner_engine.connect() as conn:
        antes = (await conn.execute(text("SELECT count(*) FROM execucoes"))).scalar_one()
    r = await cliente.post(f"/pipelines/{b_id}/executar", headers=h)
    assert r.status_code == 409, r.text
    assert r.json()["erro"]["detalhes"][0]["mensagem"] == b_voo
    async with owner_engine.connect() as conn:
        depois = (await conn.execute(text("SELECT count(*) FROM execucoes"))).scalar_one()
    assert depois == antes  # no pre-created dependency was left behind
