"""Re-running an execution: the whole run, or from a node onwards.

What these tests protect, and why:
- the ORIGINAL execution is never reopened (it is the evidence of the failure);
- re-running from X redoes X even with an identical fingerprint, otherwise the button would return
  the snapshot one wanted to discard and look as if it had worked;
- X's upstream is reused, not recomputed;
- the stretch's external effects are NAMED before any trigger (that is the UI warning);
- a credential parameter must be provided again, because the persisted value is the redaction mark.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao

pytestmark = pytest.mark.integration

DEF_SIMPLES = {
    "nos": [
        {"id": "n1", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1),(2),(3)) t(n)"},
        {"id": "f", "tipo": "guiada", "config": {"op": {"tipo": "filtrar", "condicao": "n > 1"}}},
    ],
    "arestas": [{"de": "n1", "para": "f"}],
}


async def _publicar(cliente, h, nome: str, definicao: dict = DEF_SIMPLES) -> str:
    pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    return pid


async def test_reexecutar_cria_execucao_nova_e_preserva_a_original(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="rex1@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h, f"rex-inteira-{uuid.uuid4().hex[:6]}")
    orig = await aguardar_execucao(
        cliente, (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()["id"], h
    )
    assert orig["status"] == "concluida"

    r = await cliente.post(f"/execucoes/{orig['id']}/reexecutar", headers=h)
    assert r.status_code == 200
    nova = await aguardar_execucao(cliente, r.json()["id"], h)

    # A NEW execution, linked to the original, never the same row rewritten.
    assert nova["id"] != orig["id"]
    assert nova["origem"] == "reexecucao"
    assert nova["reexecucao_de"] == orig["id"]
    assert nova["reexecucao_desde_no"] is None
    assert nova["versao"] == orig["versao"]  # the EXECUTED version, not "the latest"

    # The original stays intact: it is the evidence of what happened.
    de_novo = (await cliente.get(f"/execucoes/{orig['id']}", headers=h)).json()
    assert de_novo["status"] == "concluida" and de_novo["origem"] == "manual"


async def test_reexecutar_desde_no_forca_o_no_e_reaproveita_o_montante(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="rex2@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h, f"rex-desde-{uuid.uuid4().hex[:6]}")
    orig = await aguardar_execucao(
        cliente, (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()["id"], h
    )

    r = await cliente.post(f"/execucoes/{orig['id']}/reexecutar", json={"desde_no": "f"}, headers=h)
    assert r.status_code == 200
    nova = await aguardar_execucao(cliente, r.json()["id"], h)
    assert nova["status"] == "concluida"
    nos = {n["no_id"]: n for n in nova["nos"]}

    # The upstream came from the original execution, without recomputing.
    assert nos["n1"]["reaproveitado"] is True
    assert nos["n1"]["duracao_ms"] == 0
    # The starting node RAN even though its fingerprint equals the previous execution's: that is
    # the whole point of "run from here". Without `forcar`, reuse would return it as reused and
    # nothing would have been redone.
    assert nos["f"]["reaproveitado"] is False
    assert nos["f"]["linhas"] == 2  # and the result is still correct
    assert nova["reexecucao_desde_no"] == "f"


async def test_reexecutar_a_partir_do_no_que_falhou(owner_engine, cliente):
    """The MAIN use case: the node broke, what came before is fine, redo from there.

    A failing node writes no `hash`/`fingerprint`/`snapshot`, so by definition it is not reusable;
    what must be proven is the other side: that its UPSTREAM comes ready (without reprocessing the
    source) and that it runs again itself."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="rex8@example.com", perfil="especialista"
    )
    definicao = {
        "nos": [
            {"id": "n1", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1),(2),(3)) t(n)"},
            {"id": "quebra", "tipo": "sql", "sql": "SELECT * FROM n1 WHERE nao_existe > 1"},
        ],
        "arestas": [{"de": "n1", "para": "quebra"}],
    }
    pid = await _publicar(cliente, h, f"rex-falha-{uuid.uuid4().hex[:6]}", definicao)
    orig = await aguardar_execucao(
        cliente, (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()["id"], h
    )
    assert orig["status"] == "falha"
    nos_orig = {n["no_id"]: n for n in orig["nos"]}
    assert nos_orig["quebra"]["status"] == "falha"
    assert nos_orig["quebra"]["hash"] is None  # a failing node leaves no identity

    # The plan cuts correctly even when starting from a node without a snapshot.
    p = (
        await cliente.get(f"/execucoes/{orig['id']}/reexecucao/plano?desde_no=quebra", headers=h)
    ).json()
    assert p["nos_a_executar"] == ["quebra"] and p["nos_reaproveitados"] == ["n1"]

    r = await cliente.post(
        f"/execucoes/{orig['id']}/reexecutar", json={"desde_no": "quebra"}, headers=h
    )
    assert r.status_code == 200
    nova = await aguardar_execucao(cliente, r.json()["id"], h)
    nos = {n["no_id"]: n for n in nova["nos"]}
    # The upstream came ready: the source was NOT reprocessed just to reach the broken node.
    assert nos["n1"]["reaproveitado"] is True and nos["n1"]["linhas"] == 3
    # And the broken node ran again, failing once more because the SQL is still invalid. This is the
    # right behaviour: the re-run uses the SAME published version. Fixing the SQL means editing and
    # publishing, not re-running; retry exists for TRANSIENT failures (source down).
    assert nova["status"] == "falha"
    assert nos["quebra"]["reaproveitado"] is False
    assert nos["quebra"]["erro"]

    # The original is still a failure: the new one neither "fixed" it nor erased the evidence.
    de_novo = (await cliente.get(f"/execucoes/{orig['id']}", headers=h)).json()
    assert de_novo["status"] == "falha"


async def test_plano_nomeia_os_efeitos_do_trecho(owner_engine, cliente):
    """The UI warning comes from here. Saying "there are effects" without saying WHICH becomes ignored noise."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="rex3@example.com", perfil="especialista"
    )
    cid = (
        await cliente.post(
            "/conexoes",
            json={
                "nome": f"smtp-rex-{uuid.uuid4().hex[:6]}",
                "tipo": "smtp",
                "config": {"host": "smtp.example.com", "porta": 587, "remetente": "a@b.c"},
            },
            headers=h,
        )
    ).json()["id"]
    definicao = {
        "nos": [
            {"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS n"},
            {
                "id": "mail",
                "tipo": "email",
                "config": {
                    "conexao_id": cid,
                    "modo": "unico",
                    "para": "x@y.z",
                    "assunto": "hi",
                    "corpo": "body",
                },
            },
        ],
        "arestas": [{"de": "n1", "para": "mail"}],
    }
    pid = await _publicar(cliente, h, f"rex-efeito-{uuid.uuid4().hex[:6]}", definicao)
    # Does not wait for it to run (the email node would open a socket): the plan is computed over
    # the DEFINITION, and it only needs an execution to exist. Create one and read its plan.
    ex = (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()

    p = (await cliente.get(f"/execucoes/{ex['id']}/reexecucao/plano", headers=h)).json()
    assert [e["no_id"] for e in p["efeitos"]] == ["mail"]
    assert p["efeitos"][0]["tipo"] == "email"
    # Email is NEVER idempotent: resending duplicates, and the UI must be able to say so.
    assert p["efeitos"][0]["idempotente"] is False

    # From the email node onwards, the upstream is left out and the effect stays listed.
    p2 = (
        await cliente.get(f"/execucoes/{ex['id']}/reexecucao/plano?desde_no=mail", headers=h)
    ).json()
    assert p2["nos_a_executar"] == ["mail"] and p2["nos_reaproveitados"] == ["n1"]


async def test_no_inexistente_na_versao_executada_e_recusado(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="rex4@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h, f"rex-404no-{uuid.uuid4().hex[:6]}")
    orig = await aguardar_execucao(
        cliente, (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()["id"], h
    )
    r = await cliente.post(
        f"/execucoes/{orig['id']}/reexecutar", json={"desde_no": "nao_existe"}, headers=h
    )
    # Explicit refusal: silently creating an execution that ignores the requested starting point
    # would make the UI show "re-running from X" for something that is not that.
    assert r.status_code == 409
    assert r.json()["erro"]["codigo"] == "no_inexistente"


async def test_parametro_segredo_precisa_ser_reinformado(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="rex5@example.com", perfil="especialista"
    )
    definicao = {
        "parametros": [{"nome": "senha_api", "tipo": "texto", "segredo": True}],
        "nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS n"}],
        "arestas": [],
    }
    pid = await _publicar(cliente, h, f"rex-seg-{uuid.uuid4().hex[:6]}", definicao)
    # Wait for the original to FINISH: re-running while it still runs is the same pipeline with the
    # same in-flight parameters, and the duplicate guard refuses with 409; correct, but not what
    # this test measures.
    ex = await aguardar_execucao(
        cliente,
        (
            await cliente.post(
                f"/pipelines/{pid}/executar",
                json={"parametros": {"senha_api": "abc123"}},
                headers=h,
            )
        ).json()["id"],
        h,
    )

    # Without restoring the value: refusal. The persisted value is `***` (redacted); reusing it would
    # send the MARK to the source and fail far from the cause.
    r = await cliente.post(f"/execucoes/{ex['id']}/reexecutar", headers=h)
    assert r.status_code == 400
    assert r.json()["erro"]["codigo"] == "parametro_segredo_obrigatorio"
    assert "senha_api" in r.json()["erro"]["mensagem"]
    # And the message must not carry the old value back.
    assert "abc123" not in r.json()["erro"]["mensagem"]

    r2 = await cliente.post(
        f"/execucoes/{ex['id']}/reexecutar",
        json={"parametros": {"senha_api": "novo-valor"}},
        headers=h,
    )
    assert r2.status_code == 200
    nova = await aguardar_execucao(cliente, r2.json()["id"], h)
    # Still redacted in the new execution: restoring it to run is not persisting it.
    assert nova["parametros"]["senha_api"] == "***"


async def test_rbac_reexecucao(owner_engine, cliente):
    """Re-running is EXECUTING: an operator cannot, even when they triggered it through the Portal."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="rex6@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h, f"rex-rbac-{uuid.uuid4().hex[:6]}")
    orig = await aguardar_execucao(
        cliente, (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()["id"], h
    )

    _, h_op = await usuario_logado(
        owner_engine, cliente, email="rex-op@example.com", perfil="operador"
    )
    assert (
        await cliente.post(f"/execucoes/{orig['id']}/reexecutar", headers=h_op)
    ).status_code == 403
    assert (
        await cliente.get(f"/execucoes/{orig['id']}/reexecucao/plano", headers=h_op)
    ).status_code == 403

    _, h_aud = await usuario_logado(
        owner_engine, cliente, email="rex-aud@example.com", perfil="auditor"
    )
    assert (
        await cliente.post(f"/execucoes/{orig['id']}/reexecutar", headers=h_aud)
    ).status_code == 403


async def test_reexecucao_registrada_na_trilha(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="rex7@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h, f"rex-trilha-{uuid.uuid4().hex[:6]}")
    orig = await aguardar_execucao(
        cliente, (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()["id"], h
    )
    nova_id = (
        await cliente.post(f"/execucoes/{orig['id']}/reexecutar", json={"desde_no": "f"}, headers=h)
    ).json()["id"]

    async with owner_engine.begin() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT acao, depois FROM auditoria "
                        "WHERE recurso_tipo = 'execucao' AND recurso_id = :id"
                    ),
                    {"id": uuid.UUID(nova_id)},
                )
            )
            .mappings()
            .first()
        )
    assert row is not None, "the re-run must appear in the audit trail"
    assert row["acao"] == "executar"
    depois = row["depois"]
    assert depois["origem"] == "reexecucao"
    assert depois["reexecucao_de"] == orig["id"]
    assert depois["reexecucao_desde_no"] == "f"


async def test_reexecutar_com_irma_em_voo_recusa(owner_engine, cliente, monkeypatch):
    """A re-run goes through the same choke point (`criar_execucao`) and inherits the duplicate
    guard: the original is `falha`/`concluida`, so it only collides with ANOTHER in-flight
    execution with the same parameters, and refusing is right."""
    from cflow_core.execucao import engine
    from cflow_core.execucao import reconciliacao as rec

    uid, h = await usuario_logado(
        owner_engine, cliente, email="rex-conc@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h, f"rex-conc-{uuid.uuid4().hex[:6]}")
    orig = await aguardar_execucao(
        cliente, (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()["id"], h
    )
    assert orig["status"] == "concluida"
    irma = (await engine.criar_execucao(pipeline_id=uuid.UUID(pid), ator_id=uid))["execucao_id"]

    async def _conectar(_host):
        return object()

    monkeypatch.setattr(rec, "conectar", _conectar)

    async def _viva(_c, _e, **_kw):
        return rec.Veredito(viva=True, desconhecida=False)

    monkeypatch.setattr(rec, "consultar_uma", _viva)

    r = await cliente.post(f"/execucoes/{orig['id']}/reexecutar", headers=h)
    assert r.status_code == 409, r.text
    assert r.json()["erro"]["detalhes"][0]["mensagem"] == irma
