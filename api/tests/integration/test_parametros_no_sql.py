"""`:parametro` in `sql`/`guiada` nodes (runs in DuckDB, not in the source).

The Studio advertises `:name` in ANY node (auto-declared from the text, highlighted in the editor,
listed under "Parameters used" with a preview value). Previously only the `fonte` node bound it:
a `sql` node's SQL went RAW to DuckDB, which does not understand `:name`, so the preview failed
with "invalid SQL or source" and so did the execution.
Here: preview and execution both bind, "blank = all" behaves like the source, incremental reuse
respects the value, and the value never enters the SQL text (native bind).
"""

from __future__ import annotations

import uuid

import pytest

from cflow_core.execucao import engine

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

# Union of two literal "sources" + an SQL node that filters by the parameter.
ACME_SQL = "SELECT * FROM (VALUES (1),(2)) t(id) CROSS JOIN (SELECT 'ACME' AS \"VENDOR\")"
GX_SQL = "SELECT * FROM (VALUES (3)) t(id) CROSS JOIN (SELECT 'GLOBEX' AS \"VENDOR\")"
DEF = {
    "parametros": [{"nome": "vendor", "tipo": "opcoes", "opcoes": ["ACME", "GLOBEX"]}],
    "nos": [
        {"id": "acme_src", "tipo": "sql", "sql": ACME_SQL},
        {"id": "gx_src", "tipo": "sql", "sql": GX_SQL},
        {
            "id": "uniao",
            "tipo": "sql",
            "sql": "SELECT * FROM acme_src UNION ALL SELECT * FROM gx_src",
        },
        {
            "id": "filtro",
            "tipo": "sql",
            "sql": 'SELECT * FROM uniao a WHERE a."VENDOR" = :vendor',
        },
    ],
    "arestas": [
        {"de": "acme_src", "para": "uniao"},
        {"de": "gx_src", "para": "uniao"},
        {"de": "uniao", "para": "filtro"},
    ],
}


async def _logar(owner_engine, cliente, email: str):
    return await usuario_logado(owner_engine, cliente, email=email, perfil="especialista")


async def _criar(cliente, h, nome: str) -> str:
    return (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]


async def _publicar(cliente, h, definicao: dict, nome: str) -> str:
    pid = await _criar(cliente, h, nome)
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    return pid


def _no(res, no_id: str) -> dict:
    return {n["no_id"]: n for n in res["nos"]}[no_id]


def _linhas(res, no_id: str) -> int:
    return _no(res, no_id)["linhas"]


async def test_execucao_vincula_parametro_em_no_sql(owner_engine, app_engine, cliente):
    uid, h = await _logar(owner_engine, cliente, "pns1@example.com")
    pid = await _publicar(cliente, h, DEF, "param-no-sql")
    p = uuid.UUID(pid)

    r = await engine.executar(pipeline_id=p, ator_id=uid, parametros={"vendor": "ACME"})
    assert r["status"] == "concluida"
    assert _linhas(r, "uniao") == 3 and _linhas(r, "filtro") == 2

    r2 = await engine.executar(pipeline_id=p, ator_id=uid, parametros={"vendor": "GLOBEX"})
    assert r2["status"] == "concluida" and _linhas(r2, "filtro") == 1


async def test_execucao_parametro_em_branco_traz_tudo(owner_engine, app_engine, cliente):
    """Blank = all: with no value the predicate is neutralised (it does not filter by NULL)."""
    uid, h = await _logar(owner_engine, cliente, "pns2@example.com")
    pid = await _publicar(cliente, h, DEF, "param-no-sql-blank")

    r = await engine.executar(pipeline_id=uuid.UUID(pid), ator_id=uid)
    assert r["status"] == "concluida"
    assert _linhas(r, "filtro") == 3


async def test_reuso_respeita_parametro_do_no_sql(owner_engine, app_engine, cliente):
    """An `sql` node reading `:name` is a template: changing the value must NOT reuse the previous
    snapshot. (The predecessors have identical content; only the parameter changes.)"""
    uid, h = await _logar(owner_engine, cliente, "pns3@example.com")
    pid = await _publicar(cliente, h, DEF, "param-no-sql-reuse")
    p = uuid.UUID(pid)

    r1 = await engine.executar(pipeline_id=p, ator_id=uid, parametros={"vendor": "ACME"})
    assert _no(r1, "filtro")["reaproveitado"] is False

    r2 = await engine.executar(pipeline_id=p, ator_id=uid, parametros={"vendor": "ACME"})
    assert _no(r2, "filtro")["reaproveitado"] is True  # same value -> reuse

    r3 = await engine.executar(pipeline_id=p, ator_id=uid, parametros={"vendor": "GLOBEX"})
    assert _no(r3, "filtro")["reaproveitado"] is False  # different value -> recompute
    assert _linhas(r3, "filtro") == 1
    # `uniao` reads no parameter -> still reused (its fingerprint did not change)
    assert _no(r3, "uniao")["reaproveitado"] is True


async def test_preview_no_sql_com_parametro(owner_engine, app_engine, cliente):
    """Preview of an `sql` node with `:name`: it used to fail with 400 "invalid SQL or source".
    Now it binds the supplied value; with no value, it returns everything."""
    _, h = await _logar(owner_engine, cliente, "pns4@example.com")
    pid = await _criar(cliente, h, "param-no-sql-preview")

    r = await cliente.post(
        f"/pipelines/{pid}/nos/filtro/preview",
        json={"definicao": DEF, "parametros": {"vendor": "ACME"}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["contagem"] == 2
    assert {x["VENDOR"] for x in r.json()["linhas"]} == {"ACME"}

    # no value -> "blank = all" (preview is lenient, not an error)
    r2 = await cliente.post(
        f"/pipelines/{pid}/nos/filtro/preview", json={"definicao": DEF}, headers=h
    )
    assert r2.status_code == 200 and r2.json()["contagem"] == 3


async def test_preview_usa_valor_preview_declarado(owner_engine, app_engine, cliente):
    """`valor_preview` (edited in the node's "Parameters used" block) feeds the preview without the
    front end having to send `parametros`, the same lenient path as the source."""
    _, h = await _logar(owner_engine, cliente, "pns5@example.com")
    pid = await _criar(cliente, h, "param-no-sql-vp")
    d = {**DEF, "parametros": [{**DEF["parametros"][0], "valor_preview": "GLOBEX"}]}

    r = await cliente.post(f"/pipelines/{pid}/nos/filtro/preview", json={"definicao": d}, headers=h)
    assert r.status_code == 200 and r.json()["contagem"] == 1


async def test_valor_do_parametro_nao_entra_no_sql(owner_engine, app_engine, cliente):
    """Native bind: a value carrying an SQL payload is compared as TEXT, never executed.
    (`texto` parameter; with `opcoes` validation would already reject a value outside the list.)"""
    uid, h = await _logar(owner_engine, cliente, "pns6@example.com")
    d = {**DEF, "parametros": [{"nome": "vendor", "tipo": "texto"}]}
    pid = await _publicar(cliente, h, d, "param-no-sql-injection")

    payload = "ACME' OR '1'='1"
    r = await engine.executar(
        pipeline_id=uuid.UUID(pid), ator_id=uid, parametros={"vendor": payload}
    )
    assert r["status"] == "concluida"
    assert _linhas(r, "filtro") == 0  # no VENDOR equals the payload -> 0 rows, not 3


async def test_guiada_filtrar_com_parametro(owner_engine, app_engine, cliente):
    """Guided `filtrar` operation: the condition is free text, so it also accepts `:name`.
    `gerar_sql` goes through sqlglot, which rewrites `:name` to `$name`; both spellings must bind."""
    uid, h = await _logar(owner_engine, cliente, "pns7@example.com")
    d = {
        "parametros": [{"nome": "vendor", "tipo": "texto"}],
        "nos": [
            *DEF["nos"][:3],
            {
                "id": "f",
                "tipo": "guiada",
                "config": {"op": {"tipo": "filtrar", "condicao": '"VENDOR" = :vendor'}},
            },
        ],
        "arestas": [
            {"de": "acme_src", "para": "uniao"},
            {"de": "gx_src", "para": "uniao"},
            {"de": "uniao", "para": "f"},
        ],
    }
    pid = await _publicar(cliente, h, d, "param-no-guiada")

    r = await engine.executar(
        pipeline_id=uuid.UUID(pid), ator_id=uid, parametros={"vendor": "ACME"}
    )
    assert r["status"] == "concluida" and _linhas(r, "f") == 2
