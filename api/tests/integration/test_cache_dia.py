"""Same-day report cache: `engine.execucoes_do_dia` finds a COMPLETED run from TODAY (display
timezone) with the SAME normalised parameters; it ignores another day / other parameters. It is the
basis of the 'view existing / history / generate new' dialog in the web UI.

For scheduled loads, the SAME function answers a second question, by CONTAINMENT: "is there a load
from today for unit X?", where the as-of is what we want to discover. The tests at the end of the
file cover that half - including the two refusals that make it safe (an empty filter, which would
match everything, and asking for equality and containment together)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from cflow_core.db import transacao
from cflow_core.errors import ErroAplicacao
from cflow_core.execucao import engine

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


async def _pipeline_com_param(cliente, h) -> uuid.UUID:
    pid = (await cliente.post("/pipelines", json={"nome": "cache-pln"}, headers=h)).json()["id"]
    # Declared parameter (it enters execucoes.parametros normalised) - the node does not need to
    # reference it; the cache matches by the stored PARAMETERS, not by the SQL. `:param` only
    # resolves in a source node.
    definicao = {
        "parametros": [{"nome": "operacao_id", "tipo": "inteiro", "obrigatorio": True}],
        "nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}],
        "arestas": [],
    }
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    return uuid.UUID(pid)


async def _pagina(owner_engine, nome: str = "pg-cache") -> uuid.UUID:
    pgid = uuid.uuid4()
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO paginas (id, nome) VALUES (:i, :n)"), {"i": pgid, "n": nome}
        )
    return pgid


async def test_cache_do_dia_acha_mesmos_parametros(owner_engine, app_engine, cliente):
    ator, h = await usuario_logado(
        owner_engine, cliente, email="cache@example.com", perfil="especialista"
    )
    pid = await _pipeline_com_param(cliente, h)
    pgid = await _pagina(owner_engine)

    await engine.executar(
        pipeline_id=pid, ator_id=ator, parametros={"operacao_id": 7}, pagina_id=pgid
    )

    async with transacao() as conn:
        achados = await engine.execucoes_do_dia(
            conn, pipeline_id=pid, pagina_id=pgid, parametros={"operacao_id": 7}
        )
        assert len(achados) == 1
        assert achados[0]["origem"] == "manual"
        assert achados[0]["ator_nome"]  # author filled in
        # Different parameters -> no match.
        outro = await engine.execucoes_do_dia(
            conn, pipeline_id=pid, pagina_id=pgid, parametros={"operacao_id": 9}
        )
        assert outro == []


async def test_cache_do_dia_ignora_outro_dia_e_lista_historico(owner_engine, app_engine, cliente):
    ator, h = await usuario_logado(
        owner_engine, cliente, email="cache2@example.com", perfil="especialista"
    )
    pid = await _pipeline_com_param(cliente, h)
    pgid = await _pagina(owner_engine, "pg-cache2")

    # Two runs from TODAY with the same parameters -> the day history has 2 entries.
    await engine.executar(
        pipeline_id=pid, ator_id=ator, parametros={"operacao_id": 7}, pagina_id=pgid
    )
    r2 = await engine.executar(
        pipeline_id=pid, ator_id=ator, parametros={"operacao_id": 7}, pagina_id=pgid
    )

    # Move the 2nd to 2 days ago -> it drops out of "the day".
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE execucoes SET iniciada_em = now() - interval '2 days' WHERE id = :id"),
            {"id": uuid.UUID(r2["id"])},
        )

    async with transacao() as conn:
        achados = await engine.execucoes_do_dia(
            conn, pipeline_id=pid, pagina_id=pgid, parametros={"operacao_id": 7}
        )
        assert len(achados) == 1  # only today's


async def _pipeline_operacao_e_asof(cliente, h) -> uuid.UUID:
    """A pipeline shaped like a load: `operacao_id` + a REQUIRED `data_registro` as_of with no default.

    The required as-of is what makes the containment question necessary: asking by equality would
    require giving the as-of, which is precisely the answer being sought.
    """
    pid = (await cliente.post("/pipelines", json={"nome": "load-pln"}, headers=h)).json()["id"]
    definicao = {
        "parametros": [
            {"nome": "operacao_id", "tipo": "texto", "obrigatorio": True},
            {"nome": "data_registro", "tipo": "as_of", "obrigatorio": True},
        ],
        "nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}],
        "arestas": [],
    }
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    return uuid.UUID(pid)


async def test_contencao_acha_a_carga_do_dia_sem_saber_o_asof(owner_engine, app_engine, cliente):
    """The dialog question: is there a load from today for unit 11? The as-of comes back in the answer."""
    ator, h = await usuario_logado(
        owner_engine, cliente, email="carga@example.com", perfil="especialista"
    )
    pid = await _pipeline_operacao_e_asof(cliente, h)

    await engine.executar(
        pipeline_id=pid,
        ator_id=ator,
        parametros={"operacao_id": "11", "data_registro": "2026-09-20 02:00:00"},
    )
    # Another unit on the same day: it must not leak into the answer for 11.
    await engine.executar(
        pipeline_id=pid,
        ator_id=ator,
        parametros={"operacao_id": "26", "data_registro": "2026-09-20 02:00:00"},
    )

    async with transacao() as conn:
        achados = await engine.execucoes_do_dia(
            conn, pipeline_id=pid, parametros_contidos={"operacao_id": "11"}
        )
        assert len(achados) == 1
        # This is where whoever asked takes the as-of to reuse - without it the answer is useless.
        assert achados[0]["parametros"]["data_registro"].startswith("2026-09-20")
        assert achados[0]["parametros"]["operacao_id"] == "11"

        # Without `pagina_id`: the scheduled load comes from the scheduler/API, where `pagina_id` is
        # NULL. Asking for the page here would return empty for a load that exists.
        assert (
            await engine.execucoes_do_dia(
                conn,
                pipeline_id=pid,
                pagina_id=uuid.uuid4(),
                parametros_contidos={"operacao_id": "11"},
            )
            == []
        )


async def test_contencao_coage_o_valor_como_a_execucao_guardou(owner_engine, app_engine, cliente):
    """`operacao_id` is `texto`: asking with the integer 11 must find the run stored with "11".

    Without the coercion, the filter does not match and the answer is "no load exists" - silent and
    wrong.
    """
    ator, h = await usuario_logado(
        owner_engine, cliente, email="coage@example.com", perfil="especialista"
    )
    pid = await _pipeline_operacao_e_asof(cliente, h)
    await engine.executar(
        pipeline_id=pid,
        ator_id=ator,
        parametros={"operacao_id": "11", "data_registro": "2026-09-20 02:00:00"},
    )
    async with transacao() as conn:
        achados = await engine.execucoes_do_dia(
            conn, pipeline_id=pid, parametros_contidos={"operacao_id": 11}
        )
        assert len(achados) == 1


async def test_filtro_vazio_e_as_duas_perguntas_juntas_sao_recusados(
    owner_engine, app_engine, cliente
):
    """Two refusals that prevent a plausible and wrong answer.

    `@>` with `{}` matches ANY run: an empty filter is not "no filter", it is "all" - and whoever
    asked would read that as "a load exists". And asking for equality together with containment is
    asking for two different comparisons of the same column.
    """
    ator, h = await usuario_logado(
        owner_engine, cliente, email="recusa@example.com", perfil="especialista"
    )
    pid = await _pipeline_operacao_e_asof(cliente, h)
    await engine.executar(
        pipeline_id=pid,
        ator_id=ator,
        parametros={"operacao_id": "11", "data_registro": "2026-09-20 02:00:00"},
    )
    async with transacao() as conn:
        with pytest.raises(ErroAplicacao) as vazio:
            await engine.execucoes_do_dia(conn, pipeline_id=pid, parametros_contidos={})
        assert vazio.value.codigo == "parametros_invalidos"

        with pytest.raises(ErroAplicacao) as ambas:
            await engine.execucoes_do_dia(
                conn,
                pipeline_id=pid,
                parametros={"operacao_id": "11", "data_registro": "2026-09-20 02:00:00"},
                parametros_contidos={"operacao_id": "11"},
            )
        assert ambas.value.codigo == "parametros_invalidos"
