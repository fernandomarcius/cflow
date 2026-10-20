"""CREDENTIAL parameter (`segredo: true`) and "blank = all" in the execution SOURCE.

Two fixes for the same finding: a parameter's value is persisted in `execucoes.parametros` and
written to the audit trail, which is append-only and permanent. A password there is irreversible.
Now: a credential-looking name requires `segredo: true`, and the marked value is REDACTED in
everything that persists, while still reaching the node's executor in clear text (an external API
may require the password in the authentication body).
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import make_url, text

from cflow_core.errors import ErroAplicacao
from cflow_core.execucao import agendador, engine
from cflow_core.pipelines.definicao import REDIGIDO

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

SENHA = "s3nh4-must-never-leak"


async def _logar(owner_engine, cliente, email: str):
    return await usuario_logado(owner_engine, cliente, email=email, perfil="especialista")


async def _publicar(cliente, h, definicao: dict, nome: str) -> str:
    pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 200, r.text
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    return pid


# `api_senha` is marked as secret; the node SELECTS the parameter only so the test can prove the
# REAL value reaches the runtime (real usage is in the body of an http node).
DEF_SEGREDO = {
    "parametros": [
        {"nome": "api_senha", "tipo": "texto", "segredo": True},
        {"nome": "empresa", "tipo": "inteiro"},
    ],
    "nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT :api_senha AS s, :empresa AS e"}],
    "arestas": [],
}


async def test_definicao_exige_marca_segredo_em_nome_de_credencial(
    owner_engine, app_engine, cliente
):
    _, h = await _logar(owner_engine, cliente, "sg1@example.com")
    pid = (await cliente.post("/pipelines", json={"nome": "sg-no-flag"}, headers=h)).json()["id"]
    d = {**DEF_SEGREDO, "parametros": [{"nome": "api_senha", "tipo": "texto"}]}  # no `segredo` flag

    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": d, "base_numero": 0}, headers=h
    )
    assert r.status_code == 422
    erro = r.json()["erro"]
    assert erro["codigo"] == "grafo_invalido"
    assert any("segredo" in det["mensagem"] for det in erro["detalhes"])


async def test_segredo_redigido_no_banco_e_na_trilha_mas_real_em_runtime(
    owner_engine, app_engine, cliente
):
    uid, h = await _logar(owner_engine, cliente, "sg2@example.com")
    pid = await _publicar(cliente, h, DEF_SEGREDO, "sg-redaction")

    res = await engine.executar(
        pipeline_id=uuid.UUID(pid), ator_id=uid, parametros={"api_senha": SENHA, "empresa": 7}
    )
    assert res["status"] == "concluida"

    async with owner_engine.connect() as conn:
        guardado = (
            await conn.execute(
                text("SELECT parametros FROM execucoes WHERE id = :e"), {"e": res["id"]}
            )
        ).scalar_one()
        auditado = (
            await conn.execute(
                text(
                    "SELECT depois FROM auditoria WHERE recurso_tipo='execucao' AND recurso_id = :e"
                ),
                {"e": res["id"]},
            )
        ).scalar_one()

    # persisted and audited: REDACTED; a regular parameter passes through intact
    assert guardado["api_senha"] == REDIGIDO and guardado["empresa"] == 7
    assert auditado["parametros"]["api_senha"] == REDIGIDO
    assert SENHA not in str(guardado) and SENHA not in str(auditado)

    # the runtime received the REAL value (otherwise authenticating to the external API would fail)
    det = (await cliente.get(f"/execucoes/{res['id']}/nos/n1/preview", headers=h)).json()
    assert det["amostra"][0]["s"] == SENHA


async def test_nenhuma_resposta_da_api_devolve_o_segredo(owner_engine, app_engine, cliente):
    """Sweep: the secret must not appear in any endpoint that exposes the execution."""
    uid, h = await _logar(owner_engine, cliente, "sg3@example.com")
    pid = await _publicar(cliente, h, DEF_SEGREDO, "sg-sweep")
    res = await engine.executar(
        pipeline_id=uuid.UUID(pid), ator_id=uid, parametros={"api_senha": SENHA, "empresa": 1}
    )

    for rota in (f"/execucoes/{res['id']}", f"/pipelines/{pid}/execucoes"):
        r = await cliente.get(rota, headers=h)
        assert r.status_code == 200, rota
        assert SENHA not in r.text, f"secret leaked in {rota}"

    # only the auditor/admin reads the trail, the most critical place (append-only, permanent)
    _, ha = await usuario_logado(
        owner_engine, cliente, email="sg3aud@example.com", perfil="auditor"
    )
    ra = await cliente.get("/auditoria", headers=ha)
    assert ra.status_code == 200
    assert SENHA not in ra.text, "secret leaked into the audit trail"


async def test_agendar_com_parametro_segredo_recusado(owner_engine, app_engine, cliente):
    """A schedule runs unattended, so it would have to store the value in clear text. Rejected."""
    uid, h = await _logar(owner_engine, cliente, "sg4@example.com")
    pid = await _publicar(cliente, h, DEF_SEGREDO, "sg-schedule")

    from cflow_core.db import transacao

    async with transacao() as conn:
        with pytest.raises(ErroAplicacao) as exc:
            await agendador.criar(
                conn,
                pipeline_id=uuid.UUID(pid),
                agenda={"tipo": "diaria", "horario": "03:00"},
                parametros={"api_senha": SENHA},
                criado_por=uid,
            )
        assert exc.value.codigo == "agendamento_com_segredo"

    # without the credential parameter, scheduling is still allowed
    async with transacao() as conn:
        ag = await agendador.criar(
            conn,
            pipeline_id=uuid.UUID(pid),
            agenda={"tipo": "diaria", "horario": "03:00"},
            parametros={"empresa": 1},
            criado_por=uid,
        )
    assert ag["id"]


# ---- "blank = all" in the execution SOURCE (not only in the preview) ----


async def _conexao_pg(cliente, h, nome: str) -> str:
    u = make_url(os.environ["CFLOW_DATABASE_URL"])
    return (
        await cliente.post(
            "/conexoes",
            json={
                "nome": nome,
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


async def test_fonte_com_parametro_em_branco_traz_tudo(owner_engine, app_engine, cliente):
    """Source with an optional `:alvo` left blank: `:alvo` used to go literally to the driver and the
    execution FAILED (omitted) or bound "" and returned 0 rows, while the preview showed all 3.
    Now both paths agree: the preview == execution invariant holds for an empty parameter."""
    uid, h = await _logar(owner_engine, cliente, "sg5@example.com")
    cid = await _conexao_pg(cliente, h, "pg-blank")
    d = {
        "parametros": [{"nome": "alvo", "tipo": "texto"}],
        "nos": [
            {
                "id": "n1",
                "tipo": "fonte",
                "config": {
                    "conexao_id": cid,
                    "amostra": 10,
                    "sql": "SELECT v FROM (VALUES ('a'),('b'),('c')) t(v) WHERE v = :alvo",
                },
            }
        ],
        "arestas": [],
    }
    pid = await _publicar(cliente, h, d, "source-blank")
    p = uuid.UUID(pid)

    prev = await cliente.post(f"/pipelines/{pid}/nos/n1/preview", json={"definicao": d}, headers=h)
    assert prev.json()["contagem"] == 3

    omitido = await engine.executar(pipeline_id=p, ator_id=uid)
    assert omitido["status"] == "concluida" and omitido["nos"][0]["linhas"] == 3

    vazio = await engine.executar(pipeline_id=p, ator_id=uid, parametros={"alvo": ""})
    assert vazio["status"] == "concluida" and vazio["nos"][0]["linhas"] == 3

    com_valor = await engine.executar(pipeline_id=p, ator_id=uid, parametros={"alvo": "b"})
    assert com_valor["nos"][0]["linhas"] == 1  # control: with a value it still filters


async def test_fonte_parametro_obrigatorio_em_branco_ainda_erra(owner_engine, app_engine, cliente):
    """A SCOPE (required) parameter left blank does not become "return all": it stays a loud error,
    otherwise the execution would read the whole table and produce a plausible but wrong result."""
    uid, h = await _logar(owner_engine, cliente, "sg6@example.com")
    cid = await _conexao_pg(cliente, h, "pg-required")
    d = {
        "parametros": [{"nome": "alvo", "tipo": "texto", "obrigatorio": True}],
        "nos": [
            {
                "id": "n1",
                "tipo": "fonte",
                "config": {
                    "conexao_id": cid,
                    "amostra": 10,
                    "sql": "SELECT v FROM (VALUES ('a'),('b')) t(v) WHERE v = :alvo",
                },
            }
        ],
        "arestas": [],
    }
    pid = await _publicar(cliente, h, d, "source-required-blank")

    for params in (None, {"alvo": ""}):
        with pytest.raises(ErroAplicacao) as exc:
            await engine.executar(pipeline_id=uuid.UUID(pid), ator_id=uid, parametros=params)
        assert exc.value.codigo == "parametros_invalidos"
