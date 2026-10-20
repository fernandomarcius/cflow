"""Function library: MACROs load in EVERY DuckDB session, execution AND preview.

Invariant: the SAME SQL runs in preview and execution, only the data changes. We prove that a node
calling `dobro(21)` yields 42 both in inline execution and in preview, and that the execution PINS
the version used in `execucoes.bibliotecas_versoes` (report tracing).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from cflow_core.biblioteca_funcoes import definicao as bf_definicao
from cflow_core.db import transacao
from cflow_core.errors import ErroAplicacao
from cflow_core.execucao import engine
from cflow_core.modelagem import versionado
from cflow_core.pipelines import definicao as pipe_definicao
from cflow_core.pipelines import preview
from cflow_core.pipelines import service as ps

from ._arnes_auth import semear_usuario

pytestmark = pytest.mark.integration

_T = {
    "tabela": "biblioteca_funcoes",
    "versoes": "biblioteca_funcoes_versoes",
    "idcol": "biblioteca_id",
    "recurso_tipo": "biblioteca_funcao",
}

# Simple MACRO: dobro(x) -> x*2. Used by a SQL node `SELECT dobro(21) AS v` (expects 42).
DEF_LIB = {"funcoes": [{"nome": "dobro", "args": ["x"], "corpo": "(CAST(x AS INTEGER) * 2)"}]}


async def _biblioteca_publicada(conn, dono_id) -> uuid.UUID:
    """Create + draft + publish the `dobro` library (reuses `versionado`, like the apply paths)."""
    rid = await versionado.criar(
        conn,
        tabela=_T["tabela"],
        recurso_tipo=_T["recurso_tipo"],
        nome="Arithmetic",
        descricao=None,
        dono_id=dono_id,
    )
    await versionado.salvar_rascunho(
        conn,
        versoes=_T["versoes"],
        idcol=_T["idcol"],
        recurso_tipo=_T["recurso_tipo"],
        recurso_id=rid,
        definicao=DEF_LIB,
        base_numero=0,
        autor_id=dono_id,
        validar=bf_definicao.validar,
    )
    await versionado.publicar(
        conn,
        tabela=_T["tabela"],
        versoes=_T["versoes"],
        idcol=_T["idcol"],
        recurso_tipo=_T["recurso_tipo"],
        recurso_id=rid,
        base_numero=1,
        nota="v1",
        autor_id=dono_id,
        validar=bf_definicao.validar,
    )
    return rid


async def test_execucao_carrega_macro_e_fixa_pin(owner_engine, app_engine):
    dono = await semear_usuario(owner_engine, email="bfe-exec@example.com", perfil="especialista")
    async with transacao() as conn:
        rid = await _biblioteca_publicada(conn, dono)
        pid = await ps.criar(conn, nome="usa-dobro", descricao=None, dono_id=dono)
        definicao = {
            "nos": [{"id": "n", "tipo": "sql", "sql": "SELECT dobro(21) AS v"}],
            "arestas": [],
            "bibliotecas": [str(rid)],
        }
        await ps.salvar_rascunho(
            conn, pipeline_id=pid, definicao=definicao, base_numero=0, autor_id=dono
        )
        await ps.publicar(conn, pipeline_id=pid, base_numero=1, nota="v1", autor_id=dono)

    # INLINE execution (same functions as the Temporal path). The `dobro` MACRO must be on the
    # connection before the node SQL runs -> result 42.
    res = await engine.executar(pipeline_id=pid, ator_id=dono)
    assert res["status"] == "concluida"
    no = {n["no_id"]: n for n in res["nos"]}["n"]
    assert no["linhas"] == 1

    exec_id = uuid.UUID(res["id"])
    async with app_engine.connect() as conn:
        # pin stored on the run (tracing chain of the stamped report)
        pin = (
            await conn.execute(
                text("SELECT bibliotecas_versoes FROM execucoes WHERE id = :e"), {"e": exec_id}
            )
        ).scalar_one()
        # the node snapshot used the MACRO -> v = 42
        snap = (
            await conn.execute(
                text("SELECT snapshot FROM execucao_nos WHERE execucao_id = :e AND no_id = 'n'"),
                {"e": exec_id},
            )
        ).scalar_one()
    assert pin == [{"id": str(rid), "numero": 2}]

    import duckdb

    d = duckdb.connect()
    try:
        assert d.execute("SELECT v FROM read_parquet(?)", [snap]).fetchone()[0] == 42
    finally:
        d.close()


async def test_preview_carrega_macro_igual_execucao(owner_engine, app_engine):
    dono = await semear_usuario(owner_engine, email="bfe-prev@example.com", perfil="especialista")
    async with transacao() as conn:
        rid = await _biblioteca_publicada(conn, dono)
    definicao = {
        "nos": [{"id": "n", "tipo": "sql", "sql": "SELECT dobro(21) AS v"}],
        "arestas": [],
        "bibliotecas": [str(rid)],
    }
    chave = f"prev-{uuid.uuid4()}"
    try:
        async with app_engine.connect() as conn:
            out = await preview.preview_no(conn, chave=chave, definicao=definicao, no_id="n")
        # preview == execution: same SQL with the same MACRO -> 42
        assert out["colunas"] == ["v"]
        assert out["linhas"] == [{"v": 42}]
    finally:
        preview.limpar_sessao(chave)


async def test_preview_biblioteca_sem_versao_publicada_erro_nomeado(owner_engine, app_engine):
    """A draft does not count in preview: the error must NAME the library to publish (it used to be
    'function does not exist', which sent the author hunting for a typo in the SQL)."""
    dono = await semear_usuario(owner_engine, email="bfe-naopub@example.com", perfil="especialista")
    async with transacao() as conn:
        rid = await versionado.criar(
            conn,
            tabela=_T["tabela"],
            recurso_tipo=_T["recurso_tipo"],
            nome="ImportFunctions",
            descricao=None,
            dono_id=dono,
        )
        await versionado.salvar_rascunho(  # draft ONLY, never published
            conn,
            versoes=_T["versoes"],
            idcol=_T["idcol"],
            recurso_tipo=_T["recurso_tipo"],
            recurso_id=rid,
            definicao=DEF_LIB,
            base_numero=0,
            autor_id=dono,
            validar=bf_definicao.validar,
        )
    definicao = {
        "nos": [
            {
                "id": "n",
                "tipo": "sql",
                "sql": "SELECT dobro(21) AS v",
                "config": {"bibliotecas": [str(rid)]},
            }
        ],
        "arestas": [],
    }
    chave = f"prev-{uuid.uuid4()}"
    try:
        async with app_engine.connect() as conn:
            with pytest.raises(ErroAplicacao) as e:
                await preview.preview_no(conn, chave=chave, definicao=definicao, no_id="n")
    finally:
        preview.limpar_sessao(chave)
    assert e.value.status == 400 and e.value.codigo == "biblioteca_sem_versao_publicada"
    assert "ImportFunctions" in e.value.mensagem


def test_pipeline_validar_bibliotecas_formato():
    # absent is valid (backward compatible)
    pipe_definicao.validar({"nos": [{"id": "n", "tipo": "sql", "sql": "SELECT 1"}], "arestas": []})
    # list of strings: valid
    pipe_definicao.validar(
        {
            "nos": [{"id": "n", "tipo": "sql", "sql": "SELECT 1"}],
            "arestas": [],
            "bibliotecas": [str(uuid.uuid4())],
        }
    )
    # present but not a list of strings: 422
    with pytest.raises(ErroAplicacao) as e:
        pipe_definicao.validar(
            {
                "nos": [{"id": "n", "tipo": "sql", "sql": "SELECT 1"}],
                "arestas": [],
                "bibliotecas": [123],
            }
        )
    assert e.value.status == 422 and e.value.codigo == "grafo_invalido"
