"""Execution persists runtime parameters and records them in the audit trail (same transaction)."""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import make_url, text

from cflow_core.errors import ErroAplicacao
from cflow_core.execucao import engine

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao

pytestmark = pytest.mark.integration

# Trivial pipeline (1 SQL node, runs in DuckDB without a connection) + declared parameters.
DEF = {
    "nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}],
    "arestas": [],
    "parametros": [
        {"nome": "operacao_id", "tipo": "inteiro", "obrigatorio": True},
        {"nome": "mes_ref", "tipo": "opcoes", "opcoes": ["atual", "anterior"], "default": "atual"},
    ],
}


async def _publicar(cliente, h) -> str:
    pid = (await cliente.post("/pipelines", json={"nome": "exec-params"}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": DEF, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    return pid


async def test_parametros_persistidos_e_auditados(owner_engine, app_engine, cliente):
    uid, h = await usuario_logado(
        owner_engine, cliente, email="ep1@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h)

    res = await engine.executar(
        pipeline_id=uuid.UUID(pid), ator_id=uid, parametros={"operacao_id": "43"}
    )
    exec_id = res["id"]
    assert res["status"] == "concluida"

    def _j(v):
        return json.loads(v) if isinstance(v, str) else v

    esperado = {"operacao_id": 43, "mes_ref": "atual"}
    async with app_engine.connect() as conn:
        # 1) persisted in execucoes.parametros (default applied, integer coerced)
        row = (
            await conn.execute(
                text("SELECT parametros FROM execucoes WHERE id = :i"), {"i": exec_id}
            )
        ).scalar_one()
        assert _j(row) == esperado
        # 2) recorded in the audit trail (same transaction) with the parameters in the diff
        dep = (
            await conn.execute(
                text(
                    "SELECT depois FROM auditoria WHERE recurso_tipo='execucao' AND recurso_id=:i AND acao='executar'"
                ),
                {"i": exec_id},
            )
        ).scalar_one()
        assert _j(dep)["parametros"] == esperado


async def test_parametro_obrigatorio_ausente_400(owner_engine, app_engine, cliente):
    uid, h = await usuario_logado(
        owner_engine, cliente, email="ep2@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h)
    with pytest.raises(ErroAplicacao) as exc:
        await engine.executar(
            pipeline_id=uuid.UUID(pid), ator_id=uid, parametros={}
        )  # operacao_id missing
    assert exc.value.codigo == "parametros_invalidos" and exc.value.status == 400


async def test_parametro_nao_declarado_400(owner_engine, app_engine, cliente):
    uid, h = await usuario_logado(
        owner_engine, cliente, email="ep3@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h)
    with pytest.raises(ErroAplicacao) as exc:
        await engine.executar(
            pipeline_id=uuid.UUID(pid), ator_id=uid, parametros={"operacao_id": 1, "x": 9}
        )
    assert exc.value.codigo == "parametros_invalidos"


# ---- /executar endpoint accepts {parametros}, validates at the edge and checks authz ----


async def test_endpoint_executar_com_parametros(owner_engine, app_engine, cliente):
    uid, h = await usuario_logado(
        owner_engine, cliente, email="ep4@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h)
    r = await cliente.post(
        f"/pipelines/{pid}/executar", json={"parametros": {"operacao_id": "43"}}, headers=h
    )
    assert r.status_code == 200, r.text
    exec_id = r.json()["id"]
    res = await aguardar_execucao(cliente, exec_id, h)
    assert res["status"] == "concluida"
    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT parametros FROM execucoes WHERE id = :i"), {"i": exec_id}
            )
        ).scalar_one()
    row = json.loads(row) if isinstance(row, str) else row
    assert row == {"operacao_id": 43, "mes_ref": "atual"}


async def test_endpoint_parametro_invalido_400(owner_engine, app_engine, cliente):
    uid, h = await usuario_logado(
        owner_engine, cliente, email="ep5@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h)
    r = await cliente.post(
        f"/pipelines/{pid}/executar", json={"parametros": {"intruso": 1}}, headers=h
    )
    assert r.status_code == 400 and r.json()["erro"]["codigo"] == "parametros_invalidos"


async def test_endpoint_executar_authz_negado_403(owner_engine, app_engine, cliente):
    _, dono = await usuario_logado(
        owner_engine, cliente, email="ep6a@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, dono)
    # another specialist, without permission on the resource -> generic 403
    _, outro = await usuario_logado(
        owner_engine, cliente, email="ep6b@example.com", perfil="especialista"
    )
    r = await cliente.post(
        f"/pipelines/{pid}/executar", json={"parametros": {"operacao_id": 1}}, headers=outro
    )
    assert r.status_code == 403


# ---- content-addressed reuse respects parameters (real source + derived node) ----


async def test_reuso_respeita_parametros(owner_engine, app_engine, cliente):
    uid, h = await usuario_logado(
        owner_engine, cliente, email="ep7@example.com", perfil="especialista"
    )
    u = make_url(os.environ["CFLOW_DATABASE_URL"])  # postgres connection to cflow_test itself
    cid = (
        await cliente.post(
            "/conexoes",
            json={
                "nome": "pg-a5",
                "tipo": "postgres",
                "config": {
                    "host": u.host,
                    "porta": u.port,
                    "base": u.database,
                    "usuario": u.username,
                },
                "segredo": u.password,
            },
            headers=h,
        )
    ).json()["id"]

    defi = {
        "parametros": [{"nome": "alvo", "tipo": "texto", "obrigatorio": True}],
        "nos": [
            {
                "id": "n1",
                "tipo": "fonte",
                "config": {
                    "conexao_id": cid,
                    "sql": "SELECT v FROM (VALUES ('a'),('b'),('c')) t(v) WHERE v = :alvo",
                    "amostra": 10,
                },
            },
            {"id": "d", "tipo": "sql", "sql": "SELECT count(*) AS c FROM n1"},
        ],
        "arestas": [{"de": "n1", "para": "d"}],
    }
    pid = (await cliente.post("/pipelines", json={"nome": "reuse-params"}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": defi, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    p = uuid.UUID(pid)

    def _d(res):
        return {n["no_id"]: n for n in res["nos"]}["d"]

    r1 = await engine.executar(pipeline_id=p, ator_id=uid, parametros={"alvo": "a"})
    assert _d(r1)["reaproveitado"] is False  # first time: computes

    r2 = await engine.executar(pipeline_id=p, ator_id=uid, parametros={"alvo": "a"})
    assert _d(r2)["reaproveitado"] is True  # same params -> same source content -> reuse

    r3 = await engine.executar(pipeline_id=p, ator_id=uid, parametros={"alvo": "b"})
    assert (
        _d(r3)["reaproveitado"] is False
    )  # different params -> content differs -> NO improper reuse


# ---- a 0-row source must not break a downstream SQL node (empty period -> 0, not a crash) ----


async def test_fonte_vazia_nao_quebra_no_downstream(owner_engine, app_engine, cliente):
    """A source returning 0 rows still exposes its real columns; an SQL node counting the source
    completes with count(*) = 0, not 'erro_execucao'."""
    uid, h = await usuario_logado(
        owner_engine, cliente, email="ep8@example.com", perfil="especialista"
    )
    u = make_url(os.environ["CFLOW_DATABASE_URL"])  # postgres connection to cflow_test itself
    cid = (
        await cliente.post(
            "/conexoes",
            json={
                "nome": "pg-empty",
                "tipo": "postgres",
                "config": {
                    "host": u.host,
                    "porta": u.port,
                    "base": u.database,
                    "usuario": u.username,
                },
                "segredo": u.password,
            },
            headers=h,
        )
    ).json()["id"]

    # Source returning ZERO rows, but with a well-defined column 'x' (result schema).
    defi = {
        "nos": [
            {
                "id": "fonte",
                "tipo": "fonte",
                "config": {"conexao_id": cid, "sql": "SELECT 1 AS x WHERE 1=0", "amostra": 10},
            },
            {
                "id": "agg",
                "tipo": "sql",
                "sql": "SELECT count(*) AS c FROM fonte",
            },  # references the source
        ],
        "arestas": [{"de": "fonte", "para": "agg"}],
    }
    pid = (await cliente.post("/pipelines", json={"nome": "empty-period"}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": defi, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )

    res = await engine.executar(pipeline_id=uuid.UUID(pid), ator_id=uid)
    assert res["status"] == "concluida"  # did NOT crash
    nos = {n["no_id"]: n for n in res["nos"]}
    assert nos["fonte"]["linhas"] == 0  # empty period: 0 rows in the source
    assert nos["agg"]["linhas"] == 1  # count(*) always returns 1 row (the number)

    # the SQL node read the real column and returned 0 (not 'erro_execucao')
    det = (await cliente.get(f"/execucoes/{res['id']}", headers=h)).json()
    assert det["status"] == "concluida"
    assert all(n["status"] == "concluida" for n in det["nos"])


# ---- competencia: ONE parameter becomes the :c_inicio/:c_fim window, bound in the source, persisted and audited ----


async def test_competencia_expande_janela_na_fonte_e_persiste(owner_engine, app_engine, cliente):
    """A `c` parameter of type "competencia" injects `:c_inicio`/`:c_fim` into the source SQL
    (half-open window), replacing two `data` params plus a month operator with ONE param. Checks
    persistence and auditing of BOTH expanded dates."""
    uid, h = await usuario_logado(
        owner_engine, cliente, email="ec1@example.com", perfil="especialista"
    )
    u = make_url(os.environ["CFLOW_DATABASE_URL"])  # postgres connection to cflow_test itself
    cid = (
        await cliente.post(
            "/conexoes",
            json={
                "nome": "pg-comp",
                "tipo": "postgres",
                "config": {
                    "host": u.host,
                    "porta": u.port,
                    "base": u.database,
                    "usuario": u.username,
                },
                "segredo": u.password,
            },
            headers=h,
        )
    ).json()["id"]

    # Source with 4 entries on different dates; ONLY those in May 2026 fall in [05-01, 06-01).
    # The competence dates arrive as ISO TEXT; we compare by casting text -> date
    # (:c_inicio::text::date) so asyncpg infers $1 as `text` (Postgres does the cast to date),
    # the right pattern for text binds against a `date` column.
    sql_fonte = (
        "SELECT d FROM (VALUES "
        "(DATE '2026-04-30'), (DATE '2026-05-01'), (DATE '2026-05-20'), (DATE '2026-06-01')"
        ") t(d) WHERE d >= :c_inicio::text::date AND d < :c_fim::text::date"
    )
    defi = {
        "parametros": [{"nome": "c", "tipo": "competencia", "default": "anterior"}],
        "nos": [
            {
                "id": "fonte",
                "tipo": "fonte",
                "config": {"conexao_id": cid, "sql": sql_fonte, "amostra": 10},
            },
            {"id": "agg", "tipo": "sql", "sql": "SELECT count(*) AS c FROM fonte"},
        ],
        "arestas": [{"de": "fonte", "para": "agg"}],
    }
    pid = (await cliente.post("/pipelines", json={"nome": "comp"}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": defi, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )

    res = await engine.executar(
        pipeline_id=uuid.UUID(pid), ator_id=uid, parametros={"c": "2026-05"}
    )
    assert res["status"] == "concluida"
    nos = {n["no_id"]: n for n in res["nos"]}
    assert nos["fonte"]["linhas"] == 2  # 05-01 and 05-20 (06-01 is exclusive: half-open)

    def _j(v):
        return json.loads(v) if isinstance(v, str) else v

    esperado = {"c_inicio": "2026-05-01", "c_fim": "2026-06-01"}
    async with app_engine.connect() as conn:
        # persisted with BOTH expanded dates (and WITHOUT the raw "c" key)
        row = (
            await conn.execute(
                text("SELECT parametros FROM execucoes WHERE id = :i"), {"i": res["id"]}
            )
        ).scalar_one()
        assert _j(row) == esperado
        # audited in the same transaction
        dep = (
            await conn.execute(
                text(
                    "SELECT depois FROM auditoria WHERE recurso_tipo='execucao' AND recurso_id=:i AND acao='executar'"
                ),
                {"i": res["id"]},
            )
        ).scalar_one()
        assert _j(dep)["parametros"] == esperado


async def test_competencia_anterior_resolve_janela_correta(owner_engine, app_engine, cliente):
    """ "anterior" (no value supplied) resolves to the closed month before the current one
    (America/Sao_Paulo) and expands into the two boundary dates, bound in the source without error."""
    from datetime import date

    from cflow_core.timezone import FUSO_SAO_PAULO, agora_utc, para_exibicao

    uid, h = await usuario_logado(
        owner_engine, cliente, email="ec2@example.com", perfil="especialista"
    )
    u = make_url(os.environ["CFLOW_DATABASE_URL"])
    cid = (
        await cliente.post(
            "/conexoes",
            json={
                "nome": "pg-comp2",
                "tipo": "postgres",
                "config": {
                    "host": u.host,
                    "porta": u.port,
                    "base": u.database,
                    "usuario": u.username,
                },
                "segredo": u.password,
            },
            headers=h,
        )
    ).json()["id"]

    defi = {
        "parametros": [{"nome": "c", "tipo": "competencia", "default": "anterior"}],
        "nos": [
            {
                "id": "fonte",
                "tipo": "fonte",
                "config": {
                    "conexao_id": cid,
                    "sql": "SELECT :c_inicio::text::date AS ini, :c_fim::text::date AS fim",
                    "amostra": 10,
                },
            },
        ],
        "arestas": [],
    }
    pid = (await cliente.post("/pipelines", json={"nome": "comp-ant"}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": defi, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )

    res = await engine.executar(
        pipeline_id=uuid.UUID(pid), ator_id=uid
    )  # no parameters -> default "anterior"
    assert res["status"] == "concluida"

    hoje = para_exibicao(agora_utc(), FUSO_SAO_PAULO).date()
    ano, mes = (hoje.year - 1, 12) if hoje.month == 1 else (hoje.year, hoje.month - 1)
    esperado = {
        "c_inicio": date(ano, mes, 1).isoformat(),
        "c_fim": (date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)).isoformat(),
    }

    def _j(v):
        return json.loads(v) if isinstance(v, str) else v

    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT parametros FROM execucoes WHERE id = :i"), {"i": res["id"]}
            )
        ).scalar_one()
        assert _j(row) == esperado
