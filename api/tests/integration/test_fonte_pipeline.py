"""`pipeline` source type: pipeline B reads the output (snapshot) of pipeline A.

Covers: happy path (B consumes an executed A), structural validation (origem pipeline requires
pipeline_id+no_id and forbids conexao_id) and the error case (A without a completed execution:
sem_execucao in the preview).
"""

from __future__ import annotations

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao


async def _publicar_e_executar(cliente, h, nome: str, definicao: dict) -> str:
    pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    return pid


async def test_b_le_snapshot_de_a(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="fp1@example.com", perfil="especialista"
    )
    # A: produces 2 rows in the "saida" node.
    def_a = {
        "nos": [
            {"id": "saida", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1,'x'),(2,'y')) t(n,c)"}
        ],
        "arestas": [],
    }
    a_id = await _publicar_e_executar(cliente, h, "A - source", def_a)
    ex_a = (await cliente.post(f"/pipelines/{a_id}/executar", headers=h)).json()
    det_a = await aguardar_execucao(cliente, ex_a["id"], h)
    assert det_a["status"] == "concluida"

    # B: pipeline source pointing at A's output, counting the rows.
    def_b = {
        "nos": [
            {
                "id": "src",
                "tipo": "fonte",
                "config": {"origem": "pipeline", "pipeline_id": a_id, "no_id": "saida"},
            },
            {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
        ],
        "arestas": [{"de": "src", "para": "cont"}],
    }
    b_id = await _publicar_e_executar(cliente, h, "B - consumes A", def_b)
    ex_b = (await cliente.post(f"/pipelines/{b_id}/executar", headers=h)).json()
    det_b = await aguardar_execucao(cliente, ex_b["id"], h)
    assert det_b["status"] == "concluida"
    linhas = {n["no_id"]: n["linhas"] for n in det_b["nos"]}
    assert linhas["src"] == 2  # read the 2 rows of A's snapshot
    # the count value matches what A produced
    prev = (await cliente.get(f"/execucoes/{ex_b['id']}/nos/cont/preview", headers=h)).json()
    assert int(prev["amostra"][0]["c"]) == 2


async def test_b_le_snapshot_de_a_com_select(owner_engine, cliente):
    """Pipeline source with an optional SELECT on top of the upstream output: filters/projects.
    The SELECT references the stable alias `fonte_{no_id}` (here, `fonte_src`)."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="fp4@example.com", perfil="especialista"
    )
    def_a = {
        "nos": [
            {"id": "saida", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1,'x'),(2,'y')) t(n,c)"}
        ],
        "arestas": [],
    }
    a_id = await _publicar_e_executar(cliente, h, "A - source sel", def_a)
    ex_a = (await cliente.post(f"/pipelines/{a_id}/executar", headers=h)).json()
    det_a = await aguardar_execucao(cliente, ex_a["id"], h)
    assert det_a["status"] == "concluida"

    # B: pipeline source filtering A's output (only n=1) via SELECT in the source node itself.
    def_b = {
        "nos": [
            {
                "id": "src",
                "tipo": "fonte",
                "config": {
                    "origem": "pipeline",
                    "pipeline_id": a_id,
                    "no_id": "saida",
                    "sql": 'SELECT n, c FROM "fonte_src" WHERE n = 1',
                },
            }
        ],
        "arestas": [],
    }
    b_id = await _publicar_e_executar(cliente, h, "B - filters A", def_b)
    ex_b = (await cliente.post(f"/pipelines/{b_id}/executar", headers=h)).json()
    det_b = await aguardar_execucao(cliente, ex_b["id"], h)
    assert det_b["status"] == "concluida"
    linhas = {n["no_id"]: n["linhas"] for n in det_b["nos"]}
    assert linhas["src"] == 1  # the SELECT filtered 2 -> 1 row
    prev = (await cliente.get(f"/execucoes/{ex_b['id']}/nos/src/preview", headers=h)).json()
    assert int(prev["amostra"][0]["n"]) == 1


async def test_validacao_origem_pipeline_exige_referencia(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="fp2@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "Invalid B"}, headers=h)).json()["id"]
    # origem pipeline without no_id, and also mixing conexao_id -> 2 structural errors.
    definicao = {
        "nos": [
            {
                "id": "src",
                "tipo": "fonte",
                "config": {"origem": "pipeline", "pipeline_id": "x", "conexao_id": "y"},
            }
        ],
        "arestas": [],
    }
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 422
    assert r.json()["erro"]["codigo"] == "grafo_invalido"


async def test_a_sem_execucao_concluida_preview_erro(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="fp3@example.com", perfil="especialista"
    )
    # A published but NEVER executed.
    def_a = {"nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT 1 AS n"}], "arestas": []}
    a_id = await _publicar_e_executar(cliente, h, "A without exec", def_a)
    # B references A; the source node preview must fail with sem_execucao.
    b_id = (await cliente.post("/pipelines", json={"nome": "B preview"}, headers=h)).json()["id"]
    def_b = {
        "nos": [
            {
                "id": "src",
                "tipo": "fonte",
                "config": {"origem": "pipeline", "pipeline_id": a_id, "no_id": "saida"},
            }
        ],
        "arestas": [],
    }
    r = await cliente.post(
        f"/pipelines/{b_id}/nos/src/preview", json={"definicao": def_b}, headers=h
    )
    assert r.status_code == 400
    assert r.json()["erro"]["codigo"] == "sem_execucao"


async def test_resolver_snapshot_restringe_ao_as_of_pedido(owner_engine, cliente):
    """Outside the chain, "the latest completed execution" of the source used to be that of ANY as-of.

    That is how a report could read the data of a later day and come out with different values,
    with NO error: a report for August computed with September data. (Through the API, chaining
    protects: the DAG executes the source on demand with the right parameters. The hole belongs to
    whoever calls with a partial `fontes_execucao` (chain reuse, cube/view/page), so the test is
    about `resolver_snapshot`, which is where the fix lives.)
    """
    from cflow_core.db import transacao
    from cflow_core.errors import ErroAplicacao
    from cflow_core.execucao.snapshots import resolver_snapshot

    _, h = await usuario_logado(
        owner_engine, cliente, email="fp9@example.com", perfil="especialista"
    )
    def_a = {
        "parametros": [{"nome": "as_of", "tipo": "as_of"}],
        "nos": [{"id": "saida", "tipo": "sql", "sql": "SELECT CAST(:as_of AS VARCHAR) AS moment"}],
        "arestas": [],
    }
    a_id = await _publicar_e_executar(cliente, h, "A - with as-of", def_a)
    execucoes = {}
    for moment in ("2026-08-01 00:00:00", "2026-09-01 00:00:00"):
        ex = (
            await cliente.post(
                f"/pipelines/{a_id}/executar", json={"parametros": {"as_of": moment}}, headers=h
            )
        ).json()
        assert (await aguardar_execucao(cliente, ex["id"], h))["status"] == "concluida"
        execucoes[moment] = ex["id"]

    async with transacao() as conn:
        # Without `parametros`: the latest of ANY as-of, the previous behavior, kept for callers
        # that do not pass the cut yet.
        r = await resolver_snapshot(conn, a_id, "saida")
        assert r["execucao_id"] == execucoes["2026-09-01 00:00:00"]

        # With the August cut: the August one, even though a more recent one exists.
        r = await resolver_snapshot(
            conn, a_id, "saida", parametros={"as_of": "2026-08-01 00:00:00"}
        )
        assert r["execucao_id"] == execucoes["2026-08-01 00:00:00"]

        # No execution at that cut: a LOUD error that NAMES the cut. Serving the latest of another
        # as-of is what produces the plausible, wrong output.
        try:
            await resolver_snapshot(
                conn, a_id, "saida", parametros={"as_of": "2026-07-01 00:00:00"}
            )
        except ErroAplicacao as exc:
            assert exc.codigo == "sem_execucao"
            assert "2026-07-01" in exc.mensagem
        else:
            raise AssertionError("served a snapshot of another as-of silently")


async def test_fonte_com_competencia_nao_quebra_fora_da_cadeia(owner_engine, cliente):
    """The pipeline source's as-of filter is a CUT of the already normalized context, not a second
    normalization.

    Renormalizing used to break: in the normalized context `competencia` does not exist by name
    (it became `_inicio`/`_fim`), so cutting by declared name dropped both keys and validation
    complained about a "missing required" parameter, and the execution failed. The manual route did
    not see it, because it pre-creates the whole chain; the SCHEDULER (fires without
    `fontes_execucao`) and re-execution did.
    """
    from cflow_core.db import transacao
    from cflow_core.execucao.snapshots import resolver_snapshot

    _, h = await usuario_logado(
        owner_engine, cliente, email="fp11@example.com", perfil="especialista"
    )
    def_a = {
        "parametros": [{"nome": "competencia", "tipo": "competencia", "obrigatorio": True}],
        "nos": [
            {
                "id": "saida",
                "tipo": "sql",
                "sql": "SELECT CAST(:competencia_inicio AS VARCHAR) AS ini",
            }
        ],
        "arestas": [],
    }
    a_id = await _publicar_e_executar(cliente, h, "A - by period", def_a)
    ex = (
        await cliente.post(
            f"/pipelines/{a_id}/executar",
            json={"parametros": {"competencia": "2026-07"}},
            headers=h,
        )
    ).json()
    assert (await aguardar_execucao(cliente, ex["id"], h))["status"] == "concluida"

    # The NORMALIZED context is what reaches the cut: `competencia` already became start/end.
    ctx = {"competencia_inicio": "2026-07-01", "competencia_fim": "2026-08-01"}
    async with transacao() as conn:
        r = await resolver_snapshot(conn, a_id, "saida", parametros=ctx)
        assert r["execucao_id"] == ex["id"]
