"""Per-node preview over a sample, session DuckDB."""

from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text

from ._arnes_auth import SENHA_APP, semear_usuario, usuario_logado

pytestmark = pytest.mark.integration

PID = uuid.uuid4()  # path pipeline_id (the preview works on the definition in the body)

SEGREDO = SENHA_APP


def _config_pg() -> dict:
    """Connection pointing at the TEST SUITE database, not at a fixed "cflow" one.

    The conftest redirects CFLOW_DATABASE_URL to the suite database, which is where
    `semear_usuario` writes. A fixed base name would pass on a dev database (which has users) and
    return 0 rows in CI, where it exists migrated and empty: a failure that looks like the
    preview's but belongs to the test's wiring.
    Computed at RUNTIME (not at import): the redirection happens in the session fixture.
    """
    u = urlsplit(os.environ["CFLOW_DATABASE_URL"].replace("+asyncpg", ""))
    return {
        "host": u.hostname,
        "porta": u.port or 5432,
        "base": (u.path or "/").lstrip("/"),
        "usuario": "cflow_app",
    }


async def _conexao(cliente, h) -> str:
    return (
        await cliente.post(
            "/conexoes",
            json={
                "nome": "source-pg",
                "tipo": "postgres",
                "config": _config_pg(),
                "segredo": SEGREDO,
            },
            headers=h,
        )
    ).json()["id"]


async def test_preview_sobre_amostra(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="ep@example.com", perfil="admin")
    # populate usuarios in the source (the suite database itself)
    for i in range(3):
        await semear_usuario(owner_engine, email=f"fonte{i}@example.com", perfil="operador")
    conexao_id = await _conexao(cliente, h)

    definicao = {
        "nos": [
            {
                "id": "fonte",
                "tipo": "fonte",
                "config": {
                    "conexao_id": conexao_id,
                    "sql": "SELECT perfil FROM usuarios",
                    "amostra": 100,
                },
            },
            {
                "id": "agg",
                "tipo": "sql",
                "sql": "SELECT perfil, count(*) AS qtd FROM fonte GROUP BY perfil",
            },
        ],
        "arestas": [{"de": "fonte", "para": "agg"}],
    }
    r = await cliente.post(
        f"/pipelines/{PID}/nos/agg/preview", json={"definicao": definicao}, headers=h
    )
    assert r.status_code == 200
    corpo = r.json()
    assert "perfil" in corpo["colunas"] and "qtd" in corpo["colunas"]
    assert corpo["contagem"] >= 1
    assert SEGREDO not in r.text  # the source credential never leaks into the preview


async def test_preview_sql_invalido_saneado(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="ep2@example.com", perfil="admin")
    definicao = {
        "nos": [{"id": "n", "tipo": "sql", "sql": "SELECT coluna_que_nao_existe FROM nada"}],
        "arestas": [],
    }
    r = await cliente.post(
        f"/pipelines/{PID}/nos/n/preview", json={"definicao": definicao}, headers=h
    )
    assert r.status_code == 400
    erro = r.json()["erro"]
    assert erro["codigo"] == "erro_preview"
    # the DuckDB cause goes in `detalhes` (without it the author cannot tell WHAT is wrong)
    assert any(
        "coluna_que_nao_existe" in d["mensagem"] or "nada" in d["mensagem"]
        for d in erro["detalhes"]
    )


# Business guard in PL/pgSQL: what the source returns is not an SQL error, it is a rule refusing
# the execution, and the message IS the diagnosis.
_GUARDA_DDL = """
CREATE OR REPLACE FUNCTION public.cflow_test_guard(esperado text) RETURNS integer
LANGUAGE plpgsql STABLE AS $fn$
begin
  if esperado is distinct from 'hml' then
    raise exception
      'ENVIRONMENT GUARD: pipeline published for % but the hub is in hml', esperado;
  end if;
  return 1;
end $fn$
"""


async def test_preview_mostra_a_guarda_de_negocio_da_fonte(owner_engine, app_engine, cliente):
    """A server-side `RAISE` reaches the Studio WITH its message.

    Before the fix, a guard node returned only "invalid SQL or source", and the sentence that
    solved the case ("the hub is in hml") stayed in the server log, sending the author off to
    debug a correct SQL."""
    _, h = await usuario_logado(owner_engine, cliente, email="ep3@example.com", perfil="admin")
    async with owner_engine.begin() as conn:
        await conn.execute(text(_GUARDA_DDL))
    try:
        conexao_id = await _conexao(cliente, h)
        definicao = {
            "nos": [
                {
                    "id": "guarda_amb",
                    "tipo": "fonte",
                    "config": {
                        "conexao_id": conexao_id,
                        "sql": "SELECT public.cflow_test_guard('prd') AS ambiente_ok",
                    },
                }
            ],
            "arestas": [],
        }
        r = await cliente.post(
            f"/pipelines/{PID}/nos/guarda_amb/preview", json={"definicao": definicao}, headers=h
        )
        assert r.status_code == 400
        erro = r.json()["erro"]
        assert erro["codigo"] == "erro_preview"
        detalhe = " ".join(d["mensagem"] for d in erro["detalhes"])
        assert "ENVIRONMENT GUARD" in detalhe
        assert "the hub is in hml" in detalhe
        assert SEGREDO not in r.text  # the source credential stays out of the response
    finally:
        async with owner_engine.begin() as conn:
            await conn.execute(text("DROP FUNCTION IF EXISTS public.cflow_test_guard(text)"))


async def test_preview_de_falha_de_conexao_segue_generico(owner_engine, app_engine, cliente):
    """A CONNECTION error gets no detail: the driver message quotes user and database."""
    _, h = await usuario_logado(owner_engine, cliente, email="ep4@example.com", perfil="admin")
    config = _config_pg()
    config["base"] = "database_missing_on_purpose"
    conexao_id = (
        await cliente.post(
            "/conexoes",
            json={
                "nome": "source-pg-wrong-password",
                "tipo": "postgres",
                "config": config,
                "segredo": "wrong-password-on-purpose",
            },
            headers=h,
        )
    ).json()["id"]
    definicao = {
        "nos": [
            {
                "id": "n",
                "tipo": "fonte",
                "config": {"conexao_id": conexao_id, "sql": "SELECT 1 AS x"},
            }
        ],
        "arestas": [],
    }
    r = await cliente.post(
        f"/pipelines/{PID}/nos/n/preview", json={"definicao": definicao}, headers=h
    )
    assert r.status_code == 400
    erro = r.json()["erro"]
    assert erro["codigo"] == "erro_preview"
    assert not erro["detalhes"]  # nothing from the driver leaks
    assert "cflow_app" not in r.text and "wrong-password-on-purpose" not in r.text


async def test_amostra_de_conexao_com_sql_ruim_e_400_com_causa(owner_engine, app_engine, cliente):
    """`POST /conexoes/{id}/amostra` used to return a **500 "internal error"** for the caller's own
    SQL, sending them to look for a platform defect. It lives in this file because it uses the same
    Postgres source harness (`_config_pg`), whose URL is computed at runtime."""
    _, h = await usuario_logado(owner_engine, cliente, email="ep5@example.com", perfil="admin")
    conexao_id = await _conexao(cliente, h)
    r = await cliente.post(
        f"/conexoes/{conexao_id}/amostra",
        json={"sql": "SELECT coluna_que_nao_existe FROM usuarios", "limite": 5},
        headers=h,
    )
    assert r.status_code == 400
    erro = r.json()["erro"]
    assert erro["codigo"] == "erro_consulta"
    assert any("coluna_que_nao_existe" in d["mensagem"] for d in erro["detalhes"])
    assert SEGREDO not in r.text
