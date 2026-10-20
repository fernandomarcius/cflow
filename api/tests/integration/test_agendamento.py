"""Rich scheduling: CRUD (schedule + validity + target) + the sweep fires the due ones with
parameters/page/origin='agendado'; resolves the relative period at firing time; run-now."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from cflow_core.db import transacao
from cflow_core.execucao import agendador, engine
from cflow_core.periodos import resolver_janela

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


async def _disp(pid, criado_por, parametros, pagina_id, agid):
    r = await engine.executar(
        pipeline_id=pid,
        ator_id=criado_por,
        parametros=parametros,
        pagina_id=pagina_id,
        origem="agendado",
        agendamento_id=agid,
    )
    return r["id"]


async def _publicar(cliente, h, nome, definicao):
    pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    return pid


async def _venceu(owner_engine, aid):
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE agendamentos SET proxima_em = now() - interval '1 minute' WHERE id = :id"),
            {"id": uuid.UUID(aid)},
        )


async def test_agendamento_dispara_devido(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="ag@example.com", perfil="especialista"
    )
    pid = await _publicar(
        cliente,
        h,
        "ag-pln",
        {"nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}], "arestas": []},
    )

    r = await cliente.post(
        f"/pipelines/{pid}/agendamentos",
        json={"agenda": {"tipo": "horaria", "a_cada_horas": 1}},
        headers=h,
    )
    assert r.status_code == 201
    aid = r.json()["id"]
    await _venceu(owner_engine, aid)

    assert await agendador.processar_devidos(_disp) == 1

    lst = (await cliente.get(f"/pipelines/{pid}/execucoes", headers=h)).json()
    assert len(lst["itens"]) == 1 and lst["itens"][0]["status"] == "concluida"
    async with app_engine.connect() as conn:
        origem = (
            await conn.execute(
                text("SELECT origem FROM execucoes WHERE pipeline_id = :p"), {"p": uuid.UUID(pid)}
            )
        ).scalar_one()
    assert origem == "agendado"

    # proxima_em rescheduled into the future -> does not fire again
    assert await agendador.processar_devidos(_disp) == 0


async def test_agenda_invalida(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="ag2@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "ag2"}, headers=h)).json()["id"]
    r = await cliente.post(
        f"/pipelines/{pid}/agendamentos",
        json={"agenda": {"tipo": "semanal", "horario": "06:00", "dias_semana": []}},
        headers=h,
    )
    assert r.status_code == 400 and r.json()["erro"]["codigo"] == "agenda_invalida"


async def test_agendamento_pagina_resolve_periodo_e_alimenta_cache(
    owner_engine, app_engine, cliente
):
    ator, h = await usuario_logado(
        owner_engine, cliente, email="agp@example.com", perfil="especialista"
    )
    definicao = {
        "parametros": [
            {"nome": "data_inicio", "tipo": "data"},
            {"nome": "data_fim", "tipo": "data"},
        ],
        "nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}],
        "arestas": [],
    }
    pid = await _publicar(cliente, h, "ag-pag", definicao)
    pgid = uuid.uuid4()
    async with owner_engine.begin() as conn:
        await conn.execute(text("INSERT INTO paginas (id, nome) VALUES (:i, 'pg-ag')"), {"i": pgid})

    # PAGE schedule with a relative period (previous month) -> resolved at firing time.
    body = {
        "agenda": {"tipo": "diaria", "horario": "06:00"},
        "pagina_id": str(pgid),
        "parametros": {
            "@periodo": {
                "token": "mes_anterior",
                "param_inicio": "data_inicio",
                "param_fim": "data_fim",
            }
        },
    }
    aid = (await cliente.post(f"/pipelines/{pid}/agendamentos", json=body, headers=h)).json()["id"]
    await _venceu(owner_engine, aid)
    assert await agendador.processar_devidos(_disp) == 1

    ini, fim = resolver_janela("mes_anterior")
    async with app_engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT origem, pagina_id, parametros FROM execucoes WHERE pipeline_id = :p"
                    ),
                    {"p": uuid.UUID(pid)},
                )
            )
            .mappings()
            .one()
        )
    assert row["origem"] == "agendado" and str(row["pagina_id"]) == str(pgid)
    assert row["parametros"] == {"data_inicio": ini, "data_fim": fim}

    # The operator, picking the SAME period, finds the schedule's report in the day's cache.
    async with transacao() as conn:
        achados = await engine.execucoes_do_dia(
            conn,
            pipeline_id=uuid.UUID(pid),
            pagina_id=pgid,
            parametros={"data_inicio": ini, "data_fim": fim},
        )
    assert len(achados) == 1 and achados[0]["origem"] == "agendado"


async def test_executar_agora(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="agnow@example.com", perfil="especialista"
    )
    pid = await _publicar(
        cliente,
        h,
        "ag-now",
        {"nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}], "arestas": []},
    )
    aid = (
        await cliente.post(
            f"/pipelines/{pid}/agendamentos",
            json={"agenda": {"tipo": "diaria", "horario": "23:59"}},
            headers=h,
        )
    ).json()["id"]

    r = await cliente.post(f"/agendamentos/{aid}/executar-agora", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "disparado"
    async with app_engine.connect() as conn:
        n = (
            await conn.execute(
                text("SELECT count(*) FROM execucoes WHERE pipeline_id = :p AND origem='agendado'"),
                {"p": uuid.UUID(pid)},
            )
        ).scalar_one()
    assert n == 1


async def test_agendamento_fanout_por_operacao(owner_engine, app_engine, cliente):
    # @operacoes = [2, 6] -> fan-out: one execution per operation (origin agendado).
    _, h = await usuario_logado(
        owner_engine, cliente, email="agfan@example.com", perfil="especialista"
    )
    definicao = {
        "parametros": [{"nome": "operacao_id", "tipo": "inteiro"}],
        "nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}],
        "arestas": [],
    }
    pid = await _publicar(cliente, h, "ag-fan", definicao)
    pgid, pvid = uuid.uuid4(), uuid.uuid4()
    pdef = {
        "blocos": [],
        "execucao": {"operador": [{"tipo": "operacao", "param": "operacao_id"}], "derivacoes": []},
    }
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO paginas (id, nome) VALUES (:i, 'pg-fan')"), {"i": pgid}
        )
        await conn.execute(
            text(
                "INSERT INTO pagina_versoes (id, pagina_id, numero, definicao, status) "
                "VALUES (:v, :p, 1, CAST(:d AS jsonb), 'publicada')"
            ),
            {"v": pvid, "p": pgid, "d": json.dumps(pdef)},
        )
        await conn.execute(
            text("UPDATE paginas SET versao_publicada_id = :v WHERE id = :p"),
            {"v": pvid, "p": pgid},
        )

    body = {
        "agenda": {"tipo": "diaria", "horario": "06:00"},
        "pagina_id": str(pgid),
        "parametros": {"@operacoes": [2, 6]},
    }
    aid = (await cliente.post(f"/pipelines/{pid}/agendamentos", json=body, headers=h)).json()["id"]
    await _venceu(owner_engine, aid)
    assert await agendador.processar_devidos(_disp) == 1  # 1 due schedule -> internal fan-out

    async with app_engine.connect() as conn:
        ops = (
            (
                await conn.execute(
                    text(
                        "SELECT (parametros->>'operacao_id')::int AS op FROM execucoes "
                        "WHERE pipeline_id = :p AND origem='agendado' ORDER BY op"
                    ),
                    {"p": uuid.UUID(pid)},
                )
            )
            .scalars()
            .all()
        )
    assert ops == [2, 6]


async def test_agendador_adia_enquanto_a_anterior_roda_e_dispara_quando_ela_termina(
    owner_engine, app_engine, cliente, monkeypatch
):
    """`Forbid` with a short postponement: the occurrence is neither lost nor piled up."""
    from cflow_core.execucao import reconciliacao as rec

    uid, h = await usuario_logado(
        owner_engine, cliente, email="ag-adia@example.com", perfil="especialista"
    )
    pid = await _publicar(
        cliente,
        h,
        "ag-adia",
        {"nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}], "arestas": []},
    )
    aid = (
        await cliente.post(
            f"/pipelines/{pid}/agendamentos",
            json={"agenda": {"tipo": "horaria", "a_cada_horas": 1}},
            headers=h,
        )
    ).json()["id"]
    # The previous execution (same parameters: none) still in flight, alive in Temporal.
    em_voo = (await engine.criar_execucao(pipeline_id=uuid.UUID(pid), ator_id=uid))["execucao_id"]

    async def _conectar(_host):
        return object()

    monkeypatch.setattr(rec, "conectar", _conectar)

    async def _viva(_c, _e, **_kw):
        return rec.Veredito(viva=True, desconhecida=False)

    monkeypatch.setattr(rec, "consultar_uma", _viva)

    async def _estado():
        async with app_engine.connect() as conn:
            return (
                await conn.execute(
                    text(
                        "SELECT ultimo_status, ultima_execucao_id::text, "
                        "  extract(epoch FROM proxima_em - now())/60 AS min "
                        "FROM agendamentos WHERE id = :id"
                    ),
                    {"id": uuid.UUID(aid)},
                )
            ).first()

    await _venceu(owner_engine, aid)
    assert await agendador.processar_devidos(_disp) == 1
    st, ultima, minutos = await _estado()
    assert st == "adiado (1/6)" and ultima == em_voo  # points at the one that is running
    assert 3 < minutos <= 5.1  # comes back soon, not at the next hour
    async with app_engine.connect() as conn:
        n = (
            await conn.execute(
                text("SELECT count(*) FROM execucoes WHERE pipeline_id = :p"), {"p": uuid.UUID(pid)}
            )
        ).scalar_one()
    assert n == 1  # nothing new was created

    # Second pass with the previous one still in flight: counts the postponement.
    await _venceu(owner_engine, aid)
    await agendador.processar_devidos(_disp)
    assert (await _estado())[0] == "adiado (2/6)"

    # The previous one finished: the next tick really fires.
    await engine.finalizar(execucao_id=uuid.UUID(em_voo), erro=None)
    await _venceu(owner_engine, aid)
    assert await agendador.processar_devidos(_disp) == 1
    st, _u, minutos = await _estado()
    assert st == "disparado" and minutos > 30  # back on the cron (every 1 h)


async def test_agendador_esgota_os_adiamentos_e_registra_erro(
    owner_engine, app_engine, cliente, monkeypatch
):
    from cflow_core.execucao import reconciliacao as rec

    uid, h = await usuario_logado(
        owner_engine, cliente, email="ag-teto@example.com", perfil="especialista"
    )
    pid = await _publicar(
        cliente,
        h,
        "ag-teto",
        {"nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}], "arestas": []},
    )
    aid = (
        await cliente.post(
            f"/pipelines/{pid}/agendamentos",
            json={"agenda": {"tipo": "horaria", "a_cada_horas": 1}},
            headers=h,
        )
    ).json()["id"]
    await engine.criar_execucao(pipeline_id=uuid.UUID(pid), ator_id=uid)

    async def _conectar(_host):
        return object()

    monkeypatch.setattr(rec, "conectar", _conectar)

    async def _viva(_c, _e, **_kw):
        return rec.Veredito(viva=True, desconhecida=False)

    monkeypatch.setattr(rec, "consultar_uma", _viva)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                f"UPDATE agendamentos SET ultimo_status = 'adiado ({agendador.TETO_ADIAMENTOS}/"
                f"{agendador.TETO_ADIAMENTOS})', proxima_em = now() - interval '1 minute' "
                "WHERE id = :id"
            ),
            {"id": uuid.UUID(aid)},
        )
    await agendador.processar_devidos(_disp)
    async with app_engine.connect() as conn:
        st, minutos = (
            await conn.execute(
                text(
                    "SELECT ultimo_status, extract(epoch FROM proxima_em - now())/60 "
                    "FROM agendamentos WHERE id = :id"
                ),
                {"id": uuid.UUID(aid)},
            )
        ).first()
    assert st == "erro" and minutos > 30  # exhausted: back on the cron, no further postponement


# -- Nightly load: the two extensions the scheduler needed -----------------------------------
#
# A nightly load is most of the waiting time of any report, and the idea is to run it overnight
# once and reuse it during the day. There were two things the scheduler could not express, and
# both, when wrong, raise no error: they yield a snapshot nobody checks.


def test_agora_vira_o_instante_do_disparo_e_nao_o_fim_do_dia():
    """`@agora` is an instant that HAS ALREADY PASSED.

    The alternative would be `as_of: "today"`, which `resolver_corte` takes to `today 23:59:59`, a
    cutoff in the FUTURE when the occurrence is at 02:00, claiming to have seen the whole day.
    Anyone reusing that load at 15:00 would inherit the claim.
    """
    from datetime import datetime, timedelta, timezone

    sp = timezone(timedelta(hours=-3))
    agora = datetime(2026, 9, 21, 2, 0, 0, 999999, tzinfo=sp)
    assert agendador._aplicar_agora({"data_registro": "@agora"}, agora) == {
        "data_registro": "2026-09-21 02:00:00"
    }


def test_agora_e_UM_carimbo_para_a_ocorrencia_inteira():
    """The stamp is applied BEFORE the fan-out, so the night's N operations share it.

    With one instant per operation there would be N distinct snapshots per night, and "today's
    load" would stop having a single answer, which is the question the UI asks before offering
    reuse.
    """
    from datetime import datetime, timedelta, timezone

    sp = timezone(timedelta(hours=-3))
    base = agendador._aplicar_agora(
        {"data_registro": "@agora"}, datetime(2026, 9, 21, 2, tzinfo=sp)
    )
    expandido = [{**base, "operacao_id": i} for i in ("11", "26", "61")]
    assert len({x["data_registro"] for x in expandido}) == 1


def test_agora_nao_toca_no_que_nao_e_a_sentinela():
    """Only the EXACT value is replaced: period, similar text and `None` pass through untouched."""
    entrada = {"competencia": "2026-08", "quase": "@agorinha", "vazio": None}
    assert agendador._aplicar_agora(dict(entrada)) == entrada


async def test_consulta_de_operacoes_exige_os_tres_campos():
    """An incomplete `{conexao, sql, campo}` is refused right away, not turned into a night without a load."""
    with pytest.raises(Exception) as e:
        await agendador._operacoes_por_consulta({"conexao": "source_db"})
    assert "agendamento_invalido" in str(getattr(e.value, "codigo", e.value))


async def test_consulta_de_operacoes_recusa_conexao_inexistente(owner_engine):
    """A connection that does not exist is a NAMED error, not an empty list.

    An empty list would pass unnoticed: the occurrence would end with "no operations to load" and
    nobody would realize the whole night did not run.
    """
    with pytest.raises(Exception) as e:
        await agendador._operacoes_por_consulta(
            {"conexao": "no-such-connection", "sql": "SELECT 1", "campo": "x"}
        )
    assert "agendamento_invalido" in str(getattr(e.value, "codigo", e.value))
