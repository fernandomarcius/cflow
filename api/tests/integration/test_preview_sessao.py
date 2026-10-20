"""Persistent preview session: the source sample is fetched once and reused (cache)."""

from __future__ import annotations

import uuid

import pytest

import cflow_core.pipelines.preview as pv
from cflow_core.conexoes import service as cs

pytestmark = pytest.mark.integration


def _definicao(
    conexao_id: str, sql_fonte: str = "SELECT 1", sql_no: str = "SELECT count(*) AS c FROM f"
) -> dict:
    return {
        "nos": [
            {
                "id": "f",
                "tipo": "fonte",
                "config": {"conexao_id": conexao_id, "sql": sql_fonte, "amostra": 10},
            },
            {"id": "s", "tipo": "sql", "sql": sql_no},
        ],
        "arestas": [{"de": "f", "para": "s"}],
    }


async def test_amostra_cacheada_entre_previews(app_engine, monkeypatch):
    chamadas = {"n": 0}

    async def fake_amostra(cid, sql, lim, parametros=None):
        chamadas["n"] += 1
        return ["x"], [{"x": 1}, {"x": 2}]

    monkeypatch.setattr(cs, "amostra", fake_amostra)
    chave = f"teste:{uuid.uuid4()}"
    definicao = _definicao(str(uuid.uuid4()))
    try:
        async with app_engine.begin() as conn:
            r1 = await pv.preview_no(conn, chave=chave, definicao=definicao, no_id="s")
            r2 = await pv.preview_no(conn, chave=chave, definicao=definicao, no_id="s")
        assert r1["linhas"][0]["c"] == 2 and r2["linhas"][0]["c"] == 2
        assert chamadas["n"] == 1  # source fetched only once; 2nd preview = cache hit
    finally:
        pv.limpar_sessao(chave)


async def test_editar_no_derivado_nao_rebusca_fonte(app_engine, monkeypatch):
    chamadas = {"n": 0}

    async def fake_amostra(cid, sql, lim, parametros=None):
        chamadas["n"] += 1
        return ["x"], [{"x": 10}]

    monkeypatch.setattr(cs, "amostra", fake_amostra)
    chave = f"teste:{uuid.uuid4()}"
    cid = str(uuid.uuid4())
    try:
        async with app_engine.begin() as conn:
            await pv.preview_no(
                conn,
                chave=chave,
                definicao=_definicao(cid, sql_no="SELECT count(*) AS c FROM f"),
                no_id="s",
            )
            # edit only the derived node SQL -> the source is not re-fetched
            # the source is read as VARCHAR (no inference) -> explicit CAST to sum
            await pv.preview_no(
                conn,
                chave=chave,
                definicao=_definicao(cid, sql_no="SELECT sum(CAST(x AS INTEGER)) AS soma FROM f"),
                no_id="s",
            )
        assert chamadas["n"] == 1
    finally:
        pv.limpar_sessao(chave)


async def test_mudar_config_da_fonte_rebusca(app_engine, monkeypatch):
    chamadas = {"n": 0}

    async def fake_amostra(cid, sql, lim, parametros=None):
        chamadas["n"] += 1
        return ["x"], [{"x": 1}]

    monkeypatch.setattr(cs, "amostra", fake_amostra)
    chave = f"teste:{uuid.uuid4()}"
    cid = str(uuid.uuid4())
    try:
        async with app_engine.begin() as conn:
            await pv.preview_no(
                conn, chave=chave, definicao=_definicao(cid, sql_fonte="SELECT 1"), no_id="s"
            )
            # change the source QUERY -> invalidates the cache -> re-fetch
            await pv.preview_no(
                conn, chave=chave, definicao=_definicao(cid, sql_fonte="SELECT 2"), no_id="s"
            )
        assert chamadas["n"] == 2
    finally:
        pv.limpar_sessao(chave)
