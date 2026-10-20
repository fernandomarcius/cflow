"""`escrita_sql` effect node against the compose Postgres (writes to `escrita_teste` in
`cflow_test`, through a connection pointing at the same database as owner).

Two layers:
  - connector/service (deterministic, no Temporal): append / upsert twice without duplicating /
    truncate / sql mode with `_entrada`+`:param` / error -> rollback (target intact) / flag and
    type (403/400);
  - INLINE engine (`criar_execucao` + `executar_no`, no Temporal): dispatch, summary in the
    snapshot, `escrita_externa` audit in the same transaction, idempotency (re-run does not
    duplicate), empty guard + truncate; publishing without the flag -> 422.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from cflow_core.conexoes import service as cs
from cflow_core.conexoes.conectores import get_conector_escrita
from cflow_core.conexoes.conectores.postgres import montar_sql_tabela_pg
from cflow_core.errors import ErroAplicacao
from cflow_core.execucao import engine

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


def _cfg_owner(owner_engine) -> tuple[dict, str]:
    u = owner_engine.url
    return (
        {"host": u.host, "porta": int(u.port), "base": u.database, "usuario": u.username},
        u.password,
    )


async def _preparar_alvo(owner_engine, ddl_cols: str = "n int PRIMARY KEY, c text", seed=None):
    async with owner_engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS escrita_teste"))
        await conn.execute(text("DROP TABLE IF EXISTS escrita_teste.alvo"))
        await conn.execute(text(f"CREATE TABLE escrita_teste.alvo ({ddl_cols})"))
        for n, c in seed or []:
            await conn.execute(
                text("INSERT INTO escrita_teste.alvo (n, c) VALUES (:n, :c)"), {"n": n, "c": c}
            )


async def _conexao_escrita(owner_engine, cliente, h, nome, *, flag=True) -> str:
    config, senha = _cfg_owner(owner_engine)
    cid = (
        await cliente.post(
            "/conexoes",
            json={"nome": nome, "tipo": "postgres", "config": config, "segredo": senha},
            headers=h,
        )
    ).json()["id"]
    if flag:
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("UPDATE conexoes SET permitir_escrita = true WHERE id = CAST(:i AS uuid)"),
                {"i": cid},
            )
    return cid


async def _linhas_alvo(owner_engine) -> list[tuple]:
    async with owner_engine.begin() as conn:
        rows = (await conn.execute(text("SELECT n, c FROM escrita_teste.alvo ORDER BY n"))).all()
    return [(r[0], r[1]) for r in rows]


async def _resolver(app_engine, cid: str):
    """Resolve the write connector as EXECUTION does: `resolver_escrita` (credential + flag
    re-check) -> concrete connector. The write goes through the connector, the SAME surface the
    engine uses via `no_escrita_sql.executar_escrita` (there is no wrapper in `service`)."""
    async with app_engine.begin() as conn:
        tipo, config, senha = await cs.resolver_escrita(conn, uuid.UUID(cid))
    return get_conector_escrita(tipo), config, senha


# ---------------- Connector layer (resolver_escrita + connector, as execution does) ----------


async def test_append(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esc_ap@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine)
    cid = await _conexao_escrita(owner_engine, cliente, h, "esc-append")
    conector, config, senha = await _resolver(app_engine, cid)
    n = await conector.escrever_tabela(
        config,
        senha,
        schema="escrita_teste",
        tabela="alvo",
        colunas_destino=["n", "c"],
        linhas=[{"n": 1, "c": "a"}, {"n": 2, "c": "b"}],
        estrategia="append",
        chave=None,
        lote_tamanho=1000,
    )
    assert n == 2
    assert await _linhas_alvo(owner_engine) == [(1, "a"), (2, "b")]


async def test_upsert_2x_sem_duplicar(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esc_up@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine)
    cid = await _conexao_escrita(owner_engine, cliente, h, "esc-upsert")
    conector, config, senha = await _resolver(app_engine, cid)
    linhas = [{"n": 1, "c": "a"}, {"n": 2, "c": "b"}]
    for _ in range(2):  # twice -> no duplicates (upsert is idempotent on the data)
        await conector.escrever_tabela(
            config,
            senha,
            schema="escrita_teste",
            tabela="alvo",
            colunas_destino=["n", "c"],
            linhas=linhas,
            estrategia="upsert",
            chave=["n"],
            lote_tamanho=1000,
        )
    assert await _linhas_alvo(owner_engine) == [(1, "a"), (2, "b")]
    # change the value of an existing key -> UPDATE, no duplicate
    await conector.escrever_tabela(
        config,
        senha,
        schema="escrita_teste",
        tabela="alvo",
        colunas_destino=["n", "c"],
        linhas=[{"n": 2, "c": "B"}, {"n": 3, "c": "c"}],
        estrategia="upsert",
        chave=["n"],
        lote_tamanho=1000,
    )
    assert await _linhas_alvo(owner_engine) == [(1, "a"), (2, "B"), (3, "c")]


async def test_truncate_insert(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esc_tr@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine, seed=[(9, "z")])
    cid = await _conexao_escrita(owner_engine, cliente, h, "esc-trunc")
    conector, config, senha = await _resolver(app_engine, cid)
    await conector.escrever_tabela(
        config,
        senha,
        schema="escrita_teste",
        tabela="alvo",
        colunas_destino=["n", "c"],
        linhas=[{"n": 1, "c": "a"}],
        estrategia="truncate_insert",
        chave=None,
        lote_tamanho=1000,
    )
    assert await _linhas_alvo(owner_engine) == [(1, "a")]  # 'z' was truncated


async def test_modo_sql_entrada_e_param(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esc_sql@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine)
    cid = await _conexao_escrita(owner_engine, cliente, h, "esc-dml")
    conector, config, senha = await _resolver(app_engine, cid)
    sql = "INSERT INTO escrita_teste.alvo (n, c) SELECT CAST(n AS int), c || :suf FROM _entrada"
    await conector.executar_dml(
        config, senha, sql, {"suf": "-x"}, (["n", "c"], [{"n": "5", "c": "e"}])
    )
    assert await _linhas_alvo(owner_engine) == [(5, "e-x")]


async def test_erro_faz_rollback_alvo_intacto(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esc_rb@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine, seed=[(1, "x")])
    cid = await _conexao_escrita(owner_engine, cliente, h, "esc-rb")
    conector, config, senha = await _resolver(app_engine, cid)
    with pytest.raises(ErroAplicacao) as exc:  # append violates the PK (n=1 already exists)
        await conector.escrever_tabela(
            config,
            senha,
            schema="escrita_teste",
            tabela="alvo",
            colunas_destino=["n", "c"],
            linhas=[{"n": 1, "c": "a"}, {"n": 2, "c": "b"}],
            estrategia="append",
            chave=None,
            lote_tamanho=1000,
        )
    assert exc.value.codigo == "escrita_falhou"
    # rollback: no (2,'b') and no overwrite
    assert await _linhas_alvo(owner_engine) == [(1, "x")]


async def test_flag_desligada_403(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esc_nf@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine)
    cid = await _conexao_escrita(owner_engine, cliente, h, "esc-noflag", flag=False)
    # resolver_escrita re-checks the flag (the gate execution goes through) -> 403 without it.
    with pytest.raises(ErroAplicacao) as exc:
        async with app_engine.begin() as conn:
            await cs.resolver_escrita(conn, uuid.UUID(cid))
    assert exc.value.codigo == "conexao_sem_escrita" and exc.value.status == 403


async def test_tipo_incompativel_400(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esc_ti@example.com", perfil="especialista"
    )
    cid = (
        await cliente.post(
            "/conexoes",
            json={
                "nome": "esc-apirest",
                "tipo": "api_rest",
                "config": {"url": "http://x"},
                "segredo": None,
            },
            headers=h,
        )
    ).json()["id"]
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE conexoes SET permitir_escrita = true WHERE id = CAST(:i AS uuid)"),
            {"i": cid},
        )
    with pytest.raises(ErroAplicacao) as exc:
        async with app_engine.begin() as conn:
            await cs.resolver_escrita(conn, uuid.UUID(cid))
    assert exc.value.codigo == "conexao_tipo_sem_escrita" and exc.value.status == 400


# ---------------- Inline engine layer (dispatch + audit + idempotency) ----------------


async def _pipeline_escrita(cliente, h, cid, *, estrategia="append", sql_fonte=None) -> str:
    fonte = sql_fonte or "SELECT * FROM (VALUES (10,'j'),(11,'k')) t(n,c)"
    definicao = {
        "nos": [
            {"id": "src", "tipo": "sql", "sql": fonte},
            {
                "id": "w",
                "tipo": "escrita_sql",
                "config": {
                    "conexao_id": cid,
                    "modo": "tabela",
                    "schema": "escrita_teste",
                    "tabela": "alvo",
                    "estrategia": estrategia,
                    **({"chave": ["n"]} if estrategia == "upsert" else {}),
                },
            },
        ],
        "arestas": [{"de": "src", "para": "w"}],
    }
    pid = (await cliente.post("/pipelines", json={"nome": "Write E2E"}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    return pid


async def _rodar_inline(pid: str, uid: uuid.UUID) -> str:
    det = await engine.criar_execucao(pipeline_id=uuid.UUID(pid), ator_id=uid)
    eid = det["execucao_id"]
    for nid in det["ordem"]:
        await engine.executar_no(
            execucao_id=uuid.UUID(eid),
            definicao=det["definicao"],
            no_id=nid,
            parametros=det["parametros"],
        )
    return eid


async def test_engine_dispatch_grava_auditoria_e_resumo(owner_engine, app_engine, cliente):
    uid, h = await usuario_logado(
        owner_engine, cliente, email="esc_eng@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine)
    cid = await _conexao_escrita(owner_engine, cliente, h, "esc-eng")
    pid = await _pipeline_escrita(cliente, h, cid)
    await _rodar_inline(pid, uid)

    assert await _linhas_alvo(owner_engine) == [(10, "j"), (11, "k")]
    # escrita_externa trail (same transaction as 'concluida'); no secret in the diff
    async with owner_engine.begin() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT depois FROM auditoria WHERE recurso_tipo = 'escrita_externa' "
                        "ORDER BY id DESC LIMIT 1"
                    )
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    dep = row["depois"]
    assert dep["destino"] == "escrita_teste.alvo" and dep["linhas_afetadas"] == 2
    assert "senha" not in dep and "segredo" not in dep


async def test_engine_idempotente_nao_duplica(owner_engine, app_engine, cliente):
    uid, h = await usuario_logado(
        owner_engine, cliente, email="esc_idem@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine)
    cid = await _conexao_escrita(owner_engine, cliente, h, "esc-idem")
    pid = await _pipeline_escrita(cliente, h, cid)
    det = await engine.criar_execucao(pipeline_id=uuid.UUID(pid), ator_id=uid)
    eid = det["execucao_id"]
    for nid in det["ordem"]:
        await engine.executar_no(
            execucao_id=uuid.UUID(eid),
            definicao=det["definicao"],
            no_id=nid,
            parametros=det["parametros"],
        )
    assert await _linhas_alvo(owner_engine) == [(10, "j"), (11, "k")]
    # RE-run the effect node with the SAME execucao_id -> idempotency guard: reuses it and does NOT
    # repeat the effect (simulates a Temporal activity re-delivery after the external commit).
    res = await engine.executar_no(
        execucao_id=uuid.UUID(eid),
        definicao=det["definicao"],
        no_id="w",
        parametros=det["parametros"],
    )
    assert res["reaproveitado"] is True
    assert await _linhas_alvo(owner_engine) == [(10, "j"), (11, "k")]  # not duplicated


async def _forcar_marca_sem_concluir(owner_engine, eid: str) -> None:
    # Simulate a crash IN THE WINDOW between the EXTERNAL commit and the 'concluida' upsert: the
    # effect already fired (efeito_disparado=true) but the node stayed 'falha' (not 'concluida',
    # no snapshot), exactly the case the idempotency guard (concluida) does NOT cover.
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE execucao_nos SET status='falha', snapshot=NULL, efeito_disparado=true "
                "WHERE execucao_id = CAST(:e AS uuid) AND no_id='w'"
            ),
            {"e": eid},
        )


async def test_engine_reentrada_append_recusa(owner_engine, app_engine, cliente):
    """FAIL-SAFE: append already fired in an attempt that did not complete -> the re-run REFUSES
    (not idempotent) instead of duplicating. The target stays intact."""
    uid, h = await usuario_logado(
        owner_engine, cliente, email="esc_reent@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine)
    cid = await _conexao_escrita(owner_engine, cliente, h, "esc-reent")
    pid = await _pipeline_escrita(cliente, h, cid, estrategia="append")
    det = await engine.criar_execucao(pipeline_id=uuid.UUID(pid), ator_id=uid)
    eid = det["execucao_id"]
    for nid in det["ordem"]:
        await engine.executar_no(
            execucao_id=uuid.UUID(eid),
            definicao=det["definicao"],
            no_id=nid,
            parametros=det["parametros"],
        )
    assert await _linhas_alvo(owner_engine) == [(10, "j"), (11, "k")]
    await _forcar_marca_sem_concluir(owner_engine, eid)
    with pytest.raises(ErroAplicacao) as exc:
        await engine.executar_no(
            execucao_id=uuid.UUID(eid),
            definicao=det["definicao"],
            no_id="w",
            parametros=det["parametros"],
        )
    # the specific codigo (escrita_reentrada) is wrapped as erro_execucao at the node boundary, but
    # the message is preserved (it names the node).
    assert exc.value.codigo == "erro_execucao" and "'w'" in exc.value.mensagem
    assert await _linhas_alvo(owner_engine) == [(10, "j"), (11, "k")]  # NOT duplicated


async def test_engine_reentrada_upsert_permite(owner_engine, app_engine, cliente):
    """The fail-safe is strategy-aware: upsert is idempotent on the data -> after the mark, the
    re-run is ALLOWED (runs again) and does not duplicate."""
    uid, h = await usuario_logado(
        owner_engine, cliente, email="esc_reup@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine)
    cid = await _conexao_escrita(owner_engine, cliente, h, "esc-reup")
    pid = await _pipeline_escrita(cliente, h, cid, estrategia="upsert")
    det = await engine.criar_execucao(pipeline_id=uuid.UUID(pid), ator_id=uid)
    eid = det["execucao_id"]
    for nid in det["ordem"]:
        await engine.executar_no(
            execucao_id=uuid.UUID(eid),
            definicao=det["definicao"],
            no_id=nid,
            parametros=det["parametros"],
        )
    assert await _linhas_alvo(owner_engine) == [(10, "j"), (11, "k")]
    await _forcar_marca_sem_concluir(owner_engine, eid)
    res = await engine.executar_no(
        execucao_id=uuid.UUID(eid),
        definicao=det["definicao"],
        no_id="w",
        parametros=det["parametros"],
    )
    assert res["reaproveitado"] is False  # ran the effect again (idempotent, safe)
    assert await _linhas_alvo(owner_engine) == [(10, "j"), (11, "k")]  # no duplicates


async def test_engine_vazio_truncate_aborta(owner_engine, app_engine, cliente):
    uid, h = await usuario_logado(
        owner_engine, cliente, email="esc_vz@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine, seed=[(7, "survives")])
    cid = await _conexao_escrita(owner_engine, cliente, h, "esc-vz")
    # empty source + truncate_insert + permitir_vazio default false -> escrita_vazia; target preserved.
    pid = await _pipeline_escrita(
        cliente, h, cid, estrategia="truncate_insert", sql_fonte="SELECT 1 AS n, 'x' AS c WHERE 1=0"
    )
    with pytest.raises(ErroAplicacao):
        await _rodar_inline(pid, uid)
    assert await _linhas_alvo(owner_engine) == [(7, "survives")]  # truncate did NOT run


async def test_publicar_sem_flag_422(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esc_pub@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine)
    cid = await _conexao_escrita(owner_engine, cliente, h, "esc-pub", flag=False)
    definicao = {
        "nos": [
            {"id": "src", "tipo": "sql", "sql": "SELECT 1 AS n, 'a' AS c"},
            {
                "id": "w",
                "tipo": "escrita_sql",
                "config": {
                    "conexao_id": cid,
                    "modo": "tabela",
                    "schema": "escrita_teste",
                    "tabela": "alvo",
                    "estrategia": "append",
                },
            },
        ],
        "arestas": [{"de": "src", "para": "w"}],
    }
    pid = (await cliente.post("/pipelines", json={"nome": "No flag"}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    r = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    assert r.status_code == 422
    assert r.json()["erro"]["codigo"] == "conexao_efeito_invalida"


async def test_preview_dry_run_nao_toca_alvo(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esc_prev@example.com", perfil="especialista"
    )
    await _preparar_alvo(owner_engine, seed=[(1, "intact")])
    cid = await _conexao_escrita(owner_engine, cliente, h, "esc-prev")
    definicao = {
        "nos": [
            {"id": "src", "tipo": "sql", "sql": "SELECT * FROM (VALUES (10,'j'),(11,'k')) t(n,c)"},
            {
                "id": "w",
                "tipo": "escrita_sql",
                "config": {
                    "conexao_id": cid,
                    "modo": "tabela",
                    "schema": "escrita_teste",
                    "tabela": "alvo",
                    "estrategia": "append",
                },
            },
        ],
        "arestas": [{"de": "src", "para": "w"}],
    }
    pid = (await cliente.post("/pipelines", json={"nome": "Preview"}, headers=h)).json()["id"]
    r = await cliente.post(
        f"/pipelines/{pid}/nos/w/preview", json={"definicao": definicao}, headers=h
    )
    assert r.status_code == 200, r.text
    body = r.json()
    efeito = body["efeito"]
    assert efeito["tipo"] == "escrita_sql" and efeito["modo"] == "tabela"
    assert efeito["destino"] == "escrita_teste.alvo"
    assert efeito["linhas_entrada"] == 2  # input rows of the sample (not of the target)
    assert "Dry-run" in efeito["aviso"]
    # the SQL shown in preview is EXACTLY what execution would run (same builder).
    assert efeito["sql_gerado"] == montar_sql_tabela_pg(
        "escrita_teste", "alvo", ["n", "c"], "append", None
    )
    # simulated summary: status 'simulado', linhas_afetadas null
    linha = body["linhas"][0]
    assert linha["status"] == "simulado" and linha["linhas_afetadas"] is None
    # dry-run did NOT connect to the target: the table stays intact (seed only).
    assert await _linhas_alvo(owner_engine) == [(1, "intact")]
