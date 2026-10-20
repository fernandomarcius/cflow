"""Guided operations (join/filter) in the preview, with inputs coming from the edges."""

from __future__ import annotations

import uuid

import pytest

import cflow_core.pipelines.preview as pv

pytestmark = pytest.mark.integration


async def test_juntar_duas_fontes(app_engine):
    definicao = {
        "nos": [
            {"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS id1, 'a' AS va"},
            {"id": "n2", "tipo": "sql", "sql": "SELECT 1 AS id2, 'b' AS vb"},
            {
                "id": "j",
                "tipo": "guiada",
                "config": {"op": {"tipo": "juntar", "condicao": "n1.id1 = n2.id2"}},
            },
        ],
        "arestas": [{"de": "n1", "para": "j"}, {"de": "n2", "para": "j"}],
    }
    chave = f"t:{uuid.uuid4()}"
    try:
        async with app_engine.begin() as conn:
            r = await pv.preview_no(conn, chave=chave, definicao=definicao, no_id="j")
        assert r["contagem"] == 1
        assert {"va", "vb"} <= set(r["colunas"])
        assert r["linhas"][0]["va"] == "a" and r["linhas"][0]["vb"] == "b"
    finally:
        pv.limpar_sessao(chave)


async def test_filtrar(app_engine):
    definicao = {
        "nos": [
            {"id": "n1", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1),(2),(3)) AS t(n)"},
            {
                "id": "f",
                "tipo": "guiada",
                "config": {"op": {"tipo": "filtrar", "condicao": "n > 1"}},
            },
        ],
        "arestas": [{"de": "n1", "para": "f"}],
    }
    chave = f"t:{uuid.uuid4()}"
    try:
        async with app_engine.begin() as conn:
            r = await pv.preview_no(conn, chave=chave, definicao=definicao, no_id="f")
        assert r["contagem"] == 2
    finally:
        pv.limpar_sessao(chave)


async def test_colunas_disponiveis(app_engine):
    definicao = {
        "nos": [
            {"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a, 2 AS b"},
            {"id": "n2", "tipo": "sql", "sql": "SELECT * FROM n1"},
        ],
        "arestas": [{"de": "n1", "para": "n2"}],
    }
    chave = f"t:{uuid.uuid4()}"
    try:
        async with app_engine.begin() as conn:
            cols = await pv.colunas_disponiveis(conn, chave=chave, definicao=definicao, no_id="n2")
        assert cols == {"n1": ["a", "b"]}
    finally:
        pv.limpar_sessao(chave)


async def test_agrupar(app_engine):
    definicao = {
        "nos": [
            {
                "id": "n1",
                "tipo": "sql",
                "sql": "SELECT * FROM (VALUES ('x'),('x'),('y')) AS t(cat)",
            },
            {
                "id": "g",
                "tipo": "guiada",
                "config": {
                    "op": {"tipo": "agrupar", "chaves": ["cat"], "agregacoes": ["count(*) AS qtd"]}
                },
            },
        ],
        "arestas": [{"de": "n1", "para": "g"}],
    }
    chave = f"t:{uuid.uuid4()}"
    try:
        async with app_engine.begin() as conn:
            r = await pv.preview_no(conn, chave=chave, definicao=definicao, no_id="g")
        assert r["contagem"] == 2  # 'x' e 'y'
        assert {"cat", "qtd"} <= set(r["colunas"])
    finally:
        pv.limpar_sessao(chave)


async def test_uniao_empilha_tres_fontes(app_engine):
    """Join in union mode: 2+ inputs stacked by name (column order may differ)."""
    definicao = {
        "nos": [
            {"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a, 'x' AS b"},
            {"id": "n2", "tipo": "sql", "sql": "SELECT 'y' AS b, 2 AS a"},
            {"id": "n3", "tipo": "sql", "sql": "SELECT 3 AS a, 'z' AS b"},
            {"id": "u", "tipo": "guiada", "config": {"op": {"tipo": "juntar", "modo": "uniao"}}},
        ],
        "arestas": [
            {"de": "n1", "para": "u"},
            {"de": "n2", "para": "u"},
            {"de": "n3", "para": "u"},
        ],
    }
    chave = f"t:{uuid.uuid4()}"
    try:
        async with app_engine.begin() as conn:
            r = await pv.preview_no(conn, chave=chave, definicao=definicao, no_id="u")
        assert r["contagem"] == 3
        assert sorted((li["a"], li["b"]) for li in r["linhas"]) == [(1, "x"), (2, "y"), (3, "z")]
    finally:
        pv.limpar_sessao(chave)


async def test_uniao_distinta_remove_duplicatas(app_engine):
    definicao = {
        "nos": [
            {"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"},
            {"id": "n2", "tipo": "sql", "sql": "SELECT 1 AS a"},
            {
                "id": "u",
                "tipo": "guiada",
                "config": {"op": {"tipo": "juntar", "modo": "uniao", "distinto": True}},
            },
        ],
        "arestas": [{"de": "n1", "para": "u"}, {"de": "n2", "para": "u"}],
    }
    chave = f"t:{uuid.uuid4()}"
    try:
        async with app_engine.begin() as conn:
            r = await pv.preview_no(conn, chave=chave, definicao=definicao, no_id="u")
        assert r["contagem"] == 1
    finally:
        pv.limpar_sessao(chave)


async def test_uniao_colunas_diferentes_da_erro_claro(app_engine):
    from cflow_core.errors import ErroAplicacao

    definicao = {
        "nos": [
            {"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a, 2 AS b"},
            {"id": "n2", "tipo": "sql", "sql": "SELECT 3 AS a, 4 AS c"},
            {"id": "u", "tipo": "guiada", "config": {"op": {"tipo": "juntar", "modo": "uniao"}}},
        ],
        "arestas": [{"de": "n1", "para": "u"}, {"de": "n2", "para": "u"}],
    }
    chave = f"t:{uuid.uuid4()}"
    try:
        async with app_engine.begin() as conn:
            with pytest.raises(ErroAplicacao) as exc:
                await pv.preview_no(conn, chave=chave, definicao=definicao, no_id="u")
        assert exc.value.codigo == "uniao_colunas_diferentes"
        assert "n1" in exc.value.mensagem and "n2" in exc.value.mensagem
    finally:
        pv.limpar_sessao(chave)


async def test_sql_referencia_no_anterior_pelo_rotulo(app_engine):
    """The node label works as a table alias in SQL, besides the id (renaming does not break the id)."""
    definicao = {
        "nos": [
            {"id": "n7", "tipo": "sql", "rotulo": "Orders", "sql": "SELECT 1 AS a"},
            {"id": "n8", "tipo": "sql", "sql": "SELECT a FROM Orders UNION ALL SELECT a FROM n7"},
        ],
        "arestas": [{"de": "n7", "para": "n8"}],
    }
    chave = f"t:{uuid.uuid4()}"
    try:
        async with app_engine.begin() as conn:
            r = await pv.preview_no(conn, chave=chave, definicao=definicao, no_id="n8")
        assert r["contagem"] == 2  # id AND label resolve to the same table
    finally:
        pv.limpar_sessao(chave)


async def test_renomear_rotulo_derruba_alias_antigo(app_engine):
    """Renaming the node -> the old alias disappears from the session (the preview must not diverge from execution)."""
    no_sql = {"id": "n8", "tipo": "sql", "sql": "SELECT a FROM Orders"}
    d1 = {
        "nos": [
            {"id": "n7", "tipo": "sql", "rotulo": "Orders", "sql": "SELECT 1 AS a"},
            dict(no_sql),
        ],
        "arestas": [{"de": "n7", "para": "n8"}],
    }
    d2 = {
        "nos": [
            {"id": "n7", "tipo": "sql", "rotulo": "New", "sql": "SELECT 1 AS a"},
            dict(no_sql),
        ],
        "arestas": [{"de": "n7", "para": "n8"}],
    }
    chave = f"t:{uuid.uuid4()}"
    try:
        async with app_engine.begin() as conn:
            r = await pv.preview_no(conn, chave=chave, definicao=d1, no_id="n8")
            assert r["contagem"] == 1
        from cflow_core.errors import ErroAplicacao

        async with app_engine.begin() as conn:
            with pytest.raises(ErroAplicacao):  # "Orders" no longer exists in the SAME session
                await pv.preview_no(conn, chave=chave, definicao=d2, no_id="n8")
    finally:
        pv.limpar_sessao(chave)


async def test_colunas_disponiveis_incluem_rotulo(app_engine):
    definicao = {
        "nos": [
            {"id": "n7", "tipo": "sql", "rotulo": "Orders", "sql": "SELECT 1 AS a, 2 AS b"},
            {"id": "n8", "tipo": "sql", "sql": "SELECT * FROM n7"},
        ],
        "arestas": [{"de": "n7", "para": "n8"}],
    }
    chave = f"t:{uuid.uuid4()}"
    try:
        async with app_engine.begin() as conn:
            cols = await pv.colunas_disponiveis(conn, chave=chave, definicao=definicao, no_id="n8")
        assert cols == {"n7": ["a", "b"], "Orders": ["a", "b"]}
    finally:
        pv.limpar_sessao(chave)
