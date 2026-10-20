"""GLOBAL execution list (`GET /execucoes`): every pipeline on one screen, with combinable filters
(pipeline, status, origin, `[de, ate)` window), i.e. the Studio Executions screen.

Executions are seeded by direct INSERT (not through the engine) because the tests need to control
`iniciada_em`, `origem` and `status`, which running the real pipeline does not let you choose.

**The page is of RUNS, not executions.** Each item is a group `{chave, raiz, filhas, ...}`: a
pipeline with a `pipeline`-type source triggers the whole chain, and paginating by execution would
split the run across two pages. A standalone execution is a group of one, and the `itens()` helper
unpacks that for tests that only care which executions showed up.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

# Fixed moment (explicit time zone, America/Sao_Paulo): no "now", so the test neither depends on
# the clock nor crosses midnight in the middle of the suite.
BASE = datetime.fromisoformat("2026-07-15T10:00:00-03:00")


def itens(corpo: dict) -> list[dict]:
    """Flatten the groups into executions, in the order the UI draws them (root, then children)."""
    return [e for g in corpo["itens"] for e in [g["raiz"], *g["filhas"]]]


async def _pipeline(cliente, h, nome: str) -> str:
    r = await cliente.post("/pipelines", json={"nome": nome}, headers=h)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


async def _semear_execucao(
    app_engine,
    *,
    pipeline_id: str,
    iniciada_em: datetime,
    status: str = "concluida",
    origem: str = "manual",
    ator_id: uuid.UUID | None = None,
    erro: str | None = None,
    cadeia_id: uuid.UUID | None = None,
    cadeia_raiz: bool = False,
    concluida_em: datetime | None = None,
    parametros: dict | None = None,
) -> str:
    """Insert ONE execution with controlled moment/status/origin. Without `concluida_em`, a
    finished execution ends at the same instant it started."""
    async with app_engine.begin() as conn:
        return str(
            (
                await conn.execute(
                    text(
                        "INSERT INTO execucoes (pipeline_id, versao_numero, status, erro, "
                        "  iniciada_por, iniciada_em, concluida_em, origem, cadeia_id, cadeia_raiz, "
                        "  parametros) "
                        "VALUES (:p, 1, CAST(:st AS status_execucao), :er, :a, :ini, :fim, :og, "
                        "  :cad, :raiz, CAST(:par AS jsonb)) "
                        "RETURNING id"
                    ),
                    {
                        "p": pipeline_id,
                        "st": status,
                        "er": erro,
                        "a": ator_id,
                        "ini": iniciada_em,
                        "fim": None if status == "executando" else (concluida_em or iniciada_em),
                        "og": origem,
                        "cad": cadeia_id,
                        "raiz": cadeia_raiz,
                        "par": json.dumps(parametros or {}),
                    },
                )
            ).scalar_one()
        )


async def test_lista_global_junta_pipelines_e_ordena(owner_engine, app_engine, cliente):
    uid, h = await usuario_logado(
        owner_engine, cliente, email="glob@example.com", perfil="especialista"
    )
    p1 = await _pipeline(cliente, h, "Alpha")
    p2 = await _pipeline(cliente, h, "Beta")
    velha = await _semear_execucao(
        app_engine, pipeline_id=p1, iniciada_em=BASE - timedelta(hours=2), ator_id=uid
    )
    nova = await _semear_execucao(app_engine, pipeline_id=p2, iniciada_em=BASE, ator_id=uid)

    corpo = (await cliente.get("/execucoes", headers=h)).json()
    assert corpo["total"] == 2 and corpo["limite"] == 25 and corpo["offset"] == 0
    # An execution outside a chain is a group of ONE: the UI draws these rows as it always did.
    assert all(g["filhas"] == [] and g["total_no_grupo"] == 1 for g in corpo["itens"])
    # Most recent first, regardless of pipeline.
    assert [i["id"] for i in itens(corpo)] == [nova, velha]
    # The pipeline name comes along: it is the column that only exists in the "all" view.
    assert [i["pipeline_nome"] for i in itens(corpo)] == ["Beta", "Alpha"]
    assert itens(corpo)[0]["ator_nome"] == "Teste"
    assert itens(corpo)[0]["origem"] == "manual"
    assert itens(corpo)[0]["agendamento_id"] is None


async def test_filtros_combinam(owner_engine, app_engine, cliente):
    uid, h = await usuario_logado(
        owner_engine, cliente, email="filtro@example.com", perfil="especialista"
    )
    p1 = await _pipeline(cliente, h, "One")
    p2 = await _pipeline(cliente, h, "Two")
    ok_manual = await _semear_execucao(app_engine, pipeline_id=p1, iniciada_em=BASE, ator_id=uid)
    falha_agendada = await _semear_execucao(
        app_engine,
        pipeline_id=p1,
        iniciada_em=BASE - timedelta(minutes=10),
        status="falha",
        origem="agendado",
        erro="blew up",
    )
    rodando = await _semear_execucao(
        app_engine, pipeline_id=p2, iniciada_em=BASE, status="executando"
    )

    async def ids(qs: str) -> list[str]:
        r = await cliente.get(f"/execucoes?{qs}", headers=h)
        assert r.status_code == 200, r.text
        return [i["id"] for i in itens(r.json())]

    assert await ids("status=falha") == [falha_agendada]
    assert await ids("status=executando") == [rodando]
    assert await ids("origem=agendado") == [falha_agendada]
    assert await ids(f"pipeline_id={p2}") == [rodando]
    # Combination: pipeline + status (the same pipeline has one of each).
    assert await ids(f"pipeline_id={p1}&status=concluida") == [ok_manual]
    # A combination with no result is not an error: it is an empty list with total 0.
    vazio = (await cliente.get(f"/execucoes?pipeline_id={p2}&origem=agendado", headers=h)).json()
    assert vazio["itens"] == [] and vazio["total"] == 0

    # The error comes truncated so the list's status column shows the reason without downloading all.
    resp = (await cliente.get("/execucoes?status=falha", headers=h)).json()
    assert itens(resp)[0]["erro"] == "blew up"
    # `totais` counts by status IGNORING the status filter (it is each UI chip's number) and comes
    # in the same query; before, it took 3 requests that could diverge from one another.
    assert resp["totais"] == {"todas": 3, "concluida": 1, "falha": 1, "executando": 1}
    # The other filters still apply inside `totais`.
    so_p2 = (await cliente.get(f"/execucoes?pipeline_id={p2}", headers=h)).json()
    assert so_p2["totais"] == {"todas": 1, "concluida": 0, "falha": 0, "executando": 1}


async def test_janela_de_ate_tem_fim_exclusivo(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="janela@example.com", perfil="especialista"
    )
    pid = await _pipeline(cliente, h, "Window")
    # 15/07 10:00 (local) - the boundary under test is midnight of the 15th and of the 16th.
    alvo = await _semear_execucao(app_engine, pipeline_id=pid, iniciada_em=BASE)

    dentro = (
        await cliente.get(
            "/execucoes?de=2026-07-15T00:00:00-03:00&ate=2026-07-16T00:00:00-03:00", headers=h
        )
    ).json()
    assert [i["id"] for i in itens(dentro)] == [alvo] and dentro["total"] == 1

    # `ate` is EXCLUSIVE: a window ending at the very start of the 15th catches nothing.
    fora = (
        await cliente.get(
            "/execucoes?de=2026-07-14T00:00:00-03:00&ate=2026-07-15T00:00:00-03:00", headers=h
        )
    ).json()
    assert fora["itens"] == [] and fora["total"] == 0

    # A moment without a time zone is read as America/Sao_Paulo (the front end sends an offset, but
    # the API must not guess UTC: that would be a silent 3h shift).
    naive = (
        await cliente.get("/execucoes?de=2026-07-15T09:59:00&ate=2026-07-15T10:01:00", headers=h)
    ).json()
    assert [i["id"] for i in itens(naive)] == [alvo]


async def test_pipeline_sistema_so_aparece_para_admin(owner_engine, app_engine, cliente):
    _, esp = await usuario_logado(
        owner_engine, cliente, email="esp-sis@example.com", perfil="especialista"
    )
    _, adm = await usuario_logado(
        owner_engine, cliente, email="adm-sis@example.com", perfil="admin"
    )
    comum = await _pipeline(cliente, adm, "Common")
    interno = await _pipeline(cliente, adm, "Internal")
    assert (
        await cliente.patch(f"/pipelines/{interno}/sistema", json={"sistema": True}, headers=adm)
    ).status_code == 200
    await _semear_execucao(app_engine, pipeline_id=comum, iniciada_em=BASE)
    await _semear_execucao(app_engine, pipeline_id=interno, iniciada_em=BASE)

    # Specialist: the system pipeline's execution is in neither the items NOR the total (otherwise
    # pagination would reveal the existence of what the flag hides).
    visao_esp = (await cliente.get("/execucoes", headers=esp)).json()
    assert visao_esp["total"] == 1
    assert [i["pipeline_nome"] for i in itens(visao_esp)] == ["Common"]
    # Asking for the system pipeline explicitly gives a generic 403.
    assert (await cliente.get(f"/execucoes?pipeline_id={interno}", headers=esp)).status_code == 403

    visao_adm = (await cliente.get("/execucoes", headers=adm)).json()
    assert visao_adm["total"] == 2
    assert sorted(i["pipeline_nome"] for i in itens(visao_adm)) == ["Common", "Internal"]


async def test_paginacao_e_validacao(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="pag@example.com", perfil="especialista"
    )
    pid = await _pipeline(cliente, h, "Paged")
    for i in range(3):
        await _semear_execucao(app_engine, pipeline_id=pid, iniciada_em=BASE - timedelta(minutes=i))

    p1 = (await cliente.get("/execucoes?limite=2", headers=h)).json()
    assert p1["total"] == 3 and len(p1["itens"]) == 2
    p2 = (await cliente.get("/execucoes?limite=2&offset=2", headers=h)).json()
    assert len(p2["itens"]) == 1
    assert not ({i["id"] for i in itens(p1)} & {i["id"] for i in itens(p2)})

    # Validation at the edge: a value outside the enum/limit is 422, not a silently ignored filter.
    assert (await cliente.get("/execucoes?status=rodando", headers=h)).status_code == 422
    assert (await cliente.get("/execucoes?origem=magia", headers=h)).status_code == 422
    assert (await cliente.get("/execucoes?limite=101", headers=h)).status_code == 422
    assert (await cliente.get("/execucoes?offset=-1", headers=h)).status_code == 422


async def test_operador_nao_le_historico_do_studio(owner_engine, app_engine, cliente):
    _, op = await usuario_logado(
        owner_engine, cliente, email="op-hist@example.com", perfil="operador"
    )
    assert (await cliente.get("/execucoes", headers=op)).status_code == 403
    assert (await cliente.get("/execucoes")).status_code == 401


async def test_cadeia_vira_um_grupo_com_a_raiz_no_cabecalho(owner_engine, app_engine, cliente):
    """A chained trigger is ONE run in the list, not N loose rows."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="cad1@example.com", perfil="especialista"
    )
    raiz_pipe = await _pipeline(cliente, h, "Root")
    dep_pipe = await _pipeline(cliente, h, "Dependency")
    cadeia = uuid.uuid4()
    # The chained trigger runs the dependencies FIRST and the root last; seeding respects that
    # order in time, otherwise the test would prove something that does not happen in production.
    dep = await _semear_execucao(
        app_engine, pipeline_id=dep_pipe, iniciada_em=BASE - timedelta(seconds=30), cadeia_id=cadeia
    )
    raiz = await _semear_execucao(
        app_engine, pipeline_id=raiz_pipe, iniciada_em=BASE, cadeia_id=cadeia, cadeia_raiz=True
    )

    corpo = (await cliente.get("/execucoes", headers=h)).json()
    # ONE item, not two: `total` counts RUNS.
    assert corpo["total"] == 1 and len(corpo["itens"]) == 1
    g = corpo["itens"][0]
    assert g["chave"] == str(cadeia)
    assert g["raiz"]["id"] == raiz and g["raiz"]["pipeline_nome"] == "Root"
    assert [f["id"] for f in g["filhas"]] == [dep]
    assert g["total_no_grupo"] == 2 and g["casam_filtro"] == 2
    # The chips still count EXECUTIONS: whoever looks for "2 completed" wants two executions.
    assert corpo["totais"]["todas"] == 2


async def _semear_no(
    app_engine,
    *,
    execucao_id: str,
    no_id: str,
    iniciado_em: datetime | None,
    concluido_em: datetime | None,
    reaproveitado: bool = False,
) -> None:
    """Insert ONE execution node. A reused node enters as the engine writes it: without
    `iniciado_em`, only `concluido_em` (it does not go through the start mark,
    `engine._marcar_no_iniciado`)."""
    async with app_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO execucao_nos (execucao_id, no_id, status, iniciado_em, concluido_em, "
                "  reaproveitado) "
                "VALUES (:e, :n, CAST(:st AS status_execucao), :ini, :fim, :re)"
            ),
            {
                "e": execucao_id,
                "n": no_id,
                "st": "concluida" if concluido_em else "executando",
                "ini": iniciado_em,
                "fim": concluido_em,
                "re": reaproveitado,
            },
        )


async def test_filhas_vem_na_ordem_de_execucao_com_o_inicio_real(owner_engine, app_engine, cliente):
    """The chained trigger PRE-CREATES the whole run at the same instant: `iniciada_em` is the same
    for all, and the duration measured from it adds the dependencies' QUEUE to the work. The real
    start is the first node's (`comecou_em`), and children are ordered by it: the list tells the
    run in the order it happened."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="cad-ordem@example.com", perfil="especialista"
    )
    pipes = {n: await _pipeline(cliente, h, n) for n in ("Root", "Late", "Early", "Reuse", "Queue")}
    cadeia = uuid.uuid4()
    s = lambda n: BASE + timedelta(seconds=n)  # noqa: E731 - only shortens the reading of the instants
    ids: dict[str, str] = {}
    for nome in ("Late", "Early", "Reuse", "Queue"):
        ids[nome] = await _semear_execucao(
            app_engine,
            pipeline_id=pipes[nome],
            iniciada_em=BASE,  # all at the trigger instant, as pre-creation does
            status="executando" if nome == "Queue" else "concluida",
            concluida_em=s(60),
            cadeia_id=cadeia,
        )
    ids["Root"] = await _semear_execucao(
        app_engine,
        pipeline_id=pipes["Root"],
        iniciada_em=BASE,
        concluida_em=s(70),
        cadeia_id=cadeia,
        cadeia_raiz=True,
    )
    await _semear_no(
        app_engine, execucao_id=ids["Late"], no_id="n1", iniciado_em=s(40), concluido_em=s(45)
    )
    # Two nodes: the start is the FIRST one's, not the last one's.
    await _semear_no(
        app_engine, execucao_id=ids["Early"], no_id="n1", iniciado_em=s(5), concluido_em=s(20)
    )
    await _semear_no(
        app_engine, execucao_id=ids["Early"], no_id="n2", iniciado_em=s(10), concluido_em=s(30)
    )
    # A reused node has no `iniciado_em`: without its end, the whole execution would come out "without start".
    await _semear_no(
        app_engine,
        execucao_id=ids["Reuse"],
        no_id="n1",
        iniciado_em=None,
        concluido_em=s(25),
        reaproveitado=True,
    )
    await _semear_no(
        app_engine, execucao_id=ids["Root"], no_id="n1", iniciado_em=s(62), concluido_em=s(70)
    )
    # "Queue" has no node at all: it is still waiting for its dependencies.

    g = (await cliente.get("/execucoes", headers=h)).json()["itens"][0]
    assert g["raiz"]["id"] == ids["Root"]
    # EXECUTION order; whoever has not started goes last.
    assert [f["pipeline_nome"] for f in g["filhas"]] == ["Early", "Reuse", "Late", "Queue"]
    comeco = {f["pipeline_nome"]: f["comecou_em"] for f in g["filhas"]}
    assert datetime.fromisoformat(comeco["Early"]) == s(5)
    assert datetime.fromisoformat(comeco["Reuse"]) == s(25)
    assert datetime.fromisoformat(comeco["Late"]) == s(40)
    assert comeco["Queue"] is None
    assert datetime.fromisoformat(g["raiz"]["comecou_em"]) == s(62)
    # `iniciada_em` is still the TRIGGER, the same for all: the UI measures the wait from it.
    assert {datetime.fromisoformat(f["iniciada_em"]) for f in g["filhas"]} == {BASE}


async def test_filtro_mantem_a_mae_como_cabecalho_quando_so_a_filha_casa(
    owner_engine, app_engine, cliente
):
    """Filtering 'falha' with a completed root must not hide the run: the failure would be orphaned,
    without saying which trigger it came from. The parent appears as header, with its REAL status."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="cad2@example.com", perfil="especialista"
    )
    raiz_pipe = await _pipeline(cliente, h, "RootOk")
    dep_pipe = await _pipeline(cliente, h, "DepFailed")
    cadeia = uuid.uuid4()
    dep = await _semear_execucao(
        app_engine,
        pipeline_id=dep_pipe,
        iniciada_em=BASE - timedelta(seconds=30),
        status="falha",
        erro="broke",
        cadeia_id=cadeia,
    )
    raiz = await _semear_execucao(
        app_engine, pipeline_id=raiz_pipe, iniciada_em=BASE, cadeia_id=cadeia, cadeia_raiz=True
    )

    corpo = (await cliente.get("/execucoes?status=falha", headers=h)).json()
    assert corpo["total"] == 1
    g = corpo["itens"][0]
    # The parent is there, with its own status: never repainted red because of the child.
    assert g["raiz"]["id"] == raiz and g["raiz"]["status"] == "concluida"
    assert g["raiz"]["casa_filtro"] is False
    assert [f["id"] for f in g["filhas"]] == [dep]
    assert g["filhas"][0]["casa_filtro"] is True
    # This pair is what the UI uses to write "1 of 2 match the filter".
    assert g["total_no_grupo"] == 2 and g["casam_filtro"] == 1


async def test_cadeia_antiga_sem_marca_usa_a_mais_recente_como_raiz(
    owner_engine, app_engine, cliente
):
    """Chains older than the migration that added `cadeia_raiz` have no mark. Falling into a list
    without a header would be worse than the compatibility criterion: the chained trigger creates
    the dependencies first and the root last, so the most recent one is the root."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="cad3@example.com", perfil="especialista"
    )
    raiz_pipe = await _pipeline(cliente, h, "OldRoot")
    dep_pipe = await _pipeline(cliente, h, "OldDep")
    cadeia = uuid.uuid4()
    await _semear_execucao(
        app_engine, pipeline_id=dep_pipe, iniciada_em=BASE - timedelta(seconds=30), cadeia_id=cadeia
    )
    raiz = await _semear_execucao(
        app_engine, pipeline_id=raiz_pipe, iniciada_em=BASE, cadeia_id=cadeia
    )

    g = (await cliente.get("/execucoes", headers=h)).json()["itens"][0]
    assert g["raiz"]["id"] == raiz and g["raiz"]["pipeline_nome"] == "OldRoot"


async def test_cadeia_nao_vaza_execucao_de_pipeline_sistema(owner_engine, app_engine, cliente):
    """A MIXED run (common pipeline + `sistema` pipeline) must not drag the `sistema` execution to
    someone who is not an admin.

    Grouping picks the runs with the filter applied and then fetches THEIR executions: if that
    second step does not repeat the VISIBILITY cut, a visible execution pulls its hidden siblings
    along: name and status of a pipeline that `GET /execucoes?pipeline_id=` denies with 403 to the
    same user. A screen filter (status, origin, window) is another matter: that one does NOT cut
    rows, it only marks `casa_filtro`, so the parent can head the child's failure."""
    _, esp = await usuario_logado(
        owner_engine, cliente, email="esp-cad@example.com", perfil="especialista"
    )
    _, adm = await usuario_logado(
        owner_engine, cliente, email="adm-cad@example.com", perfil="admin"
    )
    comum = await _pipeline(cliente, adm, "RunCommon")
    interno = await _pipeline(cliente, adm, "RunInternal")
    assert (
        await cliente.patch(f"/pipelines/{interno}/sistema", json={"sistema": True}, headers=adm)
    ).status_code == 200

    cadeia = uuid.uuid4()
    await _semear_execucao(
        app_engine,
        pipeline_id=interno,
        iniciada_em=BASE - timedelta(seconds=30),
        cadeia_id=cadeia,
    )
    raiz = await _semear_execucao(
        app_engine, pipeline_id=comum, iniciada_em=BASE, cadeia_id=cadeia, cadeia_raiz=True
    )

    visao_esp = (await cliente.get("/execucoes", headers=esp)).json()
    nomes = [i["pipeline_nome"] for i in itens(visao_esp)]
    assert "RunInternal" not in nomes, "`sistema` pipeline execution leaked through the chain"
    assert nomes == ["RunCommon"]
    g = visao_esp["itens"][0]
    assert g["raiz"]["id"] == raiz and g["filhas"] == []
    # The group counts can also only speak of what this user may see; otherwise a "+1 pipeline"
    # would reveal the existence of what the mark hides.
    assert g["total_no_grupo"] == 1 and g["casam_filtro"] == 1

    # Admin still sees the whole run.
    visao_adm = (await cliente.get("/execucoes", headers=adm)).json()
    ga = visao_adm["itens"][0]
    assert ga["total_no_grupo"] == 2
    assert sorted(i["pipeline_nome"] for i in itens(visao_adm)) == [
        "RunCommon",
        "RunInternal",
    ]


async def test_paginacao_nao_parte_uma_corrida_ao_meio(owner_engine, app_engine, cliente):
    """Why pagination is by run: with `limite=1` over a chain of 3, paginating by execution would
    leave the parent on one page and the dependencies on the next."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="cad4@example.com", perfil="especialista"
    )
    pipes = [await _pipeline(cliente, h, f"P{i}") for i in range(3)]
    cadeia = uuid.uuid4()
    for i, p in enumerate(pipes[:-1]):
        await _semear_execucao(
            app_engine,
            pipeline_id=p,
            iniciada_em=BASE - timedelta(seconds=30 - i),
            cadeia_id=cadeia,
        )
    await _semear_execucao(
        app_engine, pipeline_id=pipes[-1], iniciada_em=BASE, cadeia_id=cadeia, cadeia_raiz=True
    )
    solta = await _semear_execucao(
        app_engine, pipeline_id=pipes[0], iniciada_em=BASE - timedelta(hours=1)
    )

    p1 = (await cliente.get("/execucoes?limite=1", headers=h)).json()
    assert p1["total"] == 2  # 2 RUNS (the chain of 3 + the standalone one), not 4 executions
    assert len(p1["itens"]) == 1
    # The run came whole, despite `limite=1`.
    assert p1["itens"][0]["total_no_grupo"] == 3
    assert len(itens(p1)) == 3

    p2 = (await cliente.get("/execucoes?limite=1&offset=1", headers=h)).json()
    assert [i["id"] for i in itens(p2)] == [solta]


async def test_busca_livre_casa_varios_campos_e_exige_todas_as_palavras(
    owner_engine, app_engine, cliente
):
    uid, h = await usuario_logado(
        owner_engine, cliente, email="busca@example.com", perfil="especialista"
    )
    load = await _pipeline(cliente, h, "LOAD ORDERS_SALES")
    ref = await _pipeline(cliente, h, "ref_01_rates")
    e_load = await _semear_execucao(
        app_engine, pipeline_id=load, iniciada_em=BASE, parametros={"batch_id": 4242}
    )
    e_ref = await _semear_execucao(
        app_engine,
        pipeline_id=ref,
        iniciada_em=BASE - timedelta(minutes=1),
        status="falha",
        erro="lake_dataset_ausente: gold/orders_sales",
        ator_id=uid,
        parametros={"batch_id": 7},
    )

    async def ids(busca: str) -> list[str]:
        r = await cliente.get("/execucoes", params={"busca": busca}, headers=h)
        assert r.status_code == 200, r.text
        return [i["id"] for i in itens(r.json())]

    assert await ids("load") == [e_load]  # pipeline name, case-insensitive
    assert await ids("dataset_ausente") == [e_ref]  # error
    assert await ids("Teste") == [e_ref]  # who triggered it
    assert await ids(e_ref[:8]) == [e_ref]  # id prefix
    # All the words (AND), each in any field.
    assert await ids("orders_sales ref_01") == [e_ref]
    assert await ids("load nonexistent") == []
    # `key=value` matches the EXACT parameter; a bare "42" does not match batch_id=4242 this way.
    assert await ids("batch_id=4242") == [e_load]
    assert await ids("batch_id=42") == []
    # Typed `%` and `_` are literal, not wildcards.
    assert await ids("%") == []
    assert await ids("LOAD_ORDERS") == []
    # The chips count within the search.
    r = (await cliente.get("/execucoes", params={"busca": "ref_01"}, headers=h)).json()
    assert r["totais"] == {"todas": 1, "concluida": 0, "falha": 1, "executando": 0}
    assert (
        await cliente.get("/execucoes", params={"busca": "x" * 201}, headers=h)
    ).status_code == 422


async def test_aba_recorta_pela_classificacao_do_pipeline(owner_engine, app_engine, cliente):
    _, adm = await usuario_logado(
        owner_engine, cliente, email="aba-adm@example.com", perfil="admin"
    )
    _, esp = await usuario_logado(
        owner_engine, cliente, email="aba-esp@example.com", perfil="especialista"
    )
    comum = await _pipeline(cliente, adm, "Common")
    bronze = await _pipeline(cliente, adm, "registry_bronze")
    load = await _pipeline(cliente, adm, "Orders Load")
    interno = await _pipeline(cliente, adm, "Internal")
    assert (
        await cliente.patch(f"/pipelines/{bronze}/camada", json={"camada": "bronze"}, headers=adm)
    ).status_code == 204
    assert (
        await cliente.patch(f"/pipelines/{load}/camada", json={"camada": "gold"}, headers=adm)
    ).status_code == 204
    assert (
        await cliente.patch(f"/pipelines/{load}/grupo", json={"grupo": "load"}, headers=adm)
    ).status_code == 204
    assert (
        await cliente.patch(f"/pipelines/{interno}/sistema", json={"sistema": True}, headers=adm)
    ).status_code == 200
    e = {}
    for i, (nome, pid) in enumerate(
        [("comum", comum), ("bronze", bronze), ("load", load), ("interno", interno)]
    ):
        e[nome] = await _semear_execucao(
            app_engine,
            pipeline_id=pid,
            iniciada_em=BASE - timedelta(minutes=i),
            status="falha" if nome == "load" else "concluida",
        )

    async def ids(qs: str, h=adm) -> list[str]:
        r = await cliente.get(f"/execucoes?{qs}", headers=h)
        assert r.status_code == 200, r.text
        return [i["id"] for i in itens(r.json())]

    assert await ids("aba=geral") == [e["comum"]]
    assert await ids("aba=bronze") == [e["bronze"]]
    # Deliberate overlap: the load pipeline is gold AND belongs to the load group.
    assert await ids("aba=gold") == [e["load"]]
    assert await ids("aba=load") == [e["load"]]
    assert await ids("aba=reference") == []
    assert await ids("aba=export") == []
    assert await ids("aba=sistema") == [e["interno"]]
    assert await ids("aba=silver") == []

    # Count per tab: with the other filters applied (status included), without the tab itself.
    corpo = (await cliente.get("/execucoes?aba=bronze", headers=adm)).json()
    assert corpo["totais_abas"] == {
        "todas": 4,
        "geral": 1,
        "sistema": 1,
        "bronze": 1,
        "silver": 0,
        "gold": 1,
        "load": 1,
        "reference": 0,
        "export": 0,
    }
    so_falha = (await cliente.get("/execucoes?status=falha", headers=adm)).json()
    assert so_falha["totais_abas"]["todas"] == 1 and so_falha["totais_abas"]["gold"] == 1
    assert so_falha["totais_abas"]["bronze"] == 0

    # A specialist does not see system pipelines: the System tab comes empty, and counts zero.
    assert await ids("aba=sistema", h=esp) == []
    visao = (await cliente.get("/execucoes", headers=esp)).json()
    assert visao["totais_abas"]["sistema"] == 0 and visao["totais_abas"]["todas"] == 3

    assert (await cliente.get("/execucoes?aba=platinum", headers=adm)).status_code == 422
