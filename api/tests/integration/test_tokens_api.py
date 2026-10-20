"""SERVICE account with an API token.

Invariants checked:
  - the token works (`Bearer cfs_...`) and appears only ONCE: the listing, the trail and later
    responses do not carry it, nor its hash;
  - revoked, expired, suspended account and an account that stopped being `servico` are refused
    with a generic 401;
  - a token only for a `servico` account (people sign in with a password and MFA); lifetime from 1
    to 365 days; a name;
  - a `servico` account does not sign in with a password, and becoming `servico` revokes open
    sessions;
  - the account triggers and follows EVERY pipeline (future and `sistema` ones included), without
    grants, and authors nothing: this resolves, for this profile, the known limit from
    `test_conta_servico.py`;
  - issuing, listing and revoking is admin-only; a service account has no access link.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from cflow_api.routes import pipelines as rotas_pipelines
from cflow_core.authz import permissoes
from cflow_core.db import transacao
from cflow_core.errors import ErroAplicacao
from cflow_core.execucao import engine as execucao
from cflow_core.pipelines import service as pipelines

from ._arnes_auth import semear_usuario, usuario_logado

pytestmark = pytest.mark.integration

SENHA_ANTIGA = "password-from-an-earlier-life-1"
DEFINICAO = {"nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}], "arestas": []}


async def _admin(owner_engine, cliente, email="adm.token@example.com"):
    return await usuario_logado(owner_engine, cliente, email=email, perfil="admin")


async def _servico(owner_engine, email="svc.token@example.com") -> uuid.UUID:
    return await semear_usuario(owner_engine, email=email, senha=SENHA_ANTIGA, perfil="servico")


async def _emitir(cliente, h, uid, nome="ci-adapter", dias=30):
    return await cliente.post(
        f"/usuarios/{uid}/tokens", json={"nome": nome, "dias": dias}, headers=h
    )


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _pipeline(nome: str, dono: uuid.UUID) -> uuid.UUID:
    async with transacao() as conn:
        pid = await pipelines.criar(conn, nome=nome, descricao=None, dono_id=dono)
        await pipelines.salvar_rascunho(
            conn, pipeline_id=pid, definicao=DEFINICAO, base_numero=0, autor_id=dono
        )
        await pipelines.publicar(conn, pipeline_id=pid, base_numero=1, nota="v1", autor_id=dono)
        return pid


async def test_token_entra_e_so_aparece_uma_vez(owner_engine, cliente):
    _, h = await _admin(owner_engine, cliente)
    uid = await _servico(owner_engine)
    r = await _emitir(cliente, h, uid)
    assert r.status_code == 201, r.text
    corpo = r.json()
    token = corpo["token"]
    assert token.startswith("cfs_") and corpo["prefixo"] == token[:10]

    eu = await cliente.get("/auth/me", headers=_bearer(token))
    assert eu.status_code == 200 and eu.json()["email"] == "svc.token@example.com"

    lista = await cliente.get(f"/usuarios/{uid}/tokens", headers=h)
    assert lista.status_code == 200 and len(lista.json()) == 1
    assert "token" not in lista.json()[0] and token not in lista.text
    assert lista.json()[0]["ultimo_uso_em"] is not None  # the /auth/me above counted

    async with owner_engine.connect() as conn:
        trilha = (
            await conn.execute(
                text("SELECT acao, depois::text FROM auditoria WHERE recurso_tipo = 'token_api'")
            )
        ).all()
        hash_ = (await conn.execute(text("SELECT token_hash FROM tokens_api"))).scalar_one()
    assert [t[0] for t in trilha] == ["criar"]
    assert corpo["prefixo"] in trilha[0][1]
    assert token not in trilha[0][1] and hash_ not in trilha[0][1] and hash_ not in lista.text


@pytest.mark.parametrize("como", ["revogado", "vencido", "suspenso", "deixou_de_ser_servico"])
async def test_token_que_nao_vale_mais_nao_entra(owner_engine, cliente, como):
    _, h = await _admin(owner_engine, cliente)
    uid = await _servico(owner_engine)
    corpo = (await _emitir(cliente, h, uid)).json()
    token = corpo["token"]
    assert (await cliente.get("/auth/me", headers=_bearer(token))).status_code == 200

    if como == "revogado":
        url = f"/usuarios/{uid}/tokens/{corpo['id']}"
        assert (await cliente.delete(url, headers=h)).status_code == 204
        assert (await cliente.delete(url, headers=h)).status_code == 204  # idempotent
    elif como == "vencido":
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("UPDATE tokens_api SET expira_em = now() - interval '1 minute'")
            )
    elif como == "suspenso":
        r = await cliente.patch(f"/usuarios/{uid}/ativo", json={"ativo": False}, headers=h)
        assert r.status_code == 204, r.text
    else:
        r = await cliente.patch(
            f"/usuarios/{uid}/perfil", json={"perfil": "especialista"}, headers=h
        )
        assert r.status_code == 204, r.text

    r = await cliente.get("/auth/me", headers=_bearer(token))
    assert r.status_code == 401 and r.json()["erro"]["codigo"] == "nao_autenticado"
    if como == "revogado":
        async with owner_engine.connect() as conn:
            n = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM auditoria "
                        "WHERE recurso_tipo = 'token_api' AND acao = 'excluir'"
                    )
                )
            ).scalar_one()
        assert n == 1  # the second revocation did not write again


async def test_token_so_para_conta_de_servico_com_nome_e_validade(owner_engine, cliente):
    _, h = await _admin(owner_engine, cliente)
    pessoa = await semear_usuario(owner_engine, email="person@example.com", perfil="especialista")
    r = await _emitir(cliente, h, pessoa)
    assert r.status_code == 409 and r.json()["erro"]["codigo"] == "token_so_para_servico"
    uid = await _servico(owner_engine)
    for dias in (0, 366):
        r = await _emitir(cliente, h, uid, dias=dias)
        assert r.status_code == 400 and r.json()["erro"]["codigo"] == "validade_invalida"
    r = await _emitir(cliente, h, uid, nome="   ")
    assert r.status_code == 400 and r.json()["erro"]["codigo"] == "nome_invalido"
    async with owner_engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM tokens_api"))).scalar_one() == 0


async def test_conta_de_servico_nao_entra_por_senha(owner_engine, cliente):
    await _servico(owner_engine)
    r = await cliente.post(
        "/auth/login", json={"email": "svc.token@example.com", "senha": SENHA_ANTIGA}
    )
    assert r.status_code == 401


async def test_virar_servico_revoga_as_sessoes_abertas(owner_engine, cliente):
    _, h = await _admin(owner_engine, cliente)
    uid, h_pessoa = await usuario_logado(
        owner_engine, cliente, email="vira.servico@example.com", perfil="especialista"
    )
    assert (await cliente.get("/auth/me", headers=h_pessoa)).status_code == 200
    r = await cliente.patch(f"/usuarios/{uid}/perfil", json={"perfil": "servico"}, headers=h)
    assert r.status_code == 204, r.text
    assert (await cliente.get("/auth/me", headers=h_pessoa)).status_code == 401


async def test_servico_alcanca_todo_pipeline_sem_concessao_e_nao_autora(
    owner_engine, cliente, monkeypatch
):
    """The service account reaches `executar` on EVERY pipeline, without grants, including those
    created AFTER it and those flagged `sistema`, and still authors nothing. The earlier rule
    (per-pipeline permission) made a client see "pipeline does not exist" because the listing only
    returned what had been granted one by one."""
    admin, h = await _admin(owner_engine, cliente)
    pid_a = await _pipeline("svc_old", admin)
    uid = await _servico(owner_engine)
    hs = _bearer((await _emitir(cliente, h, uid)).json()["token"])
    # Created after the account, with no grant at all; one of them is `sistema`.
    pid_b = await _pipeline("svc_future", admin)
    pid_s = await _pipeline("svc_sistema", admin)
    async with transacao() as conn:
        await conn.execute(text("UPDATE pipelines SET sistema = true WHERE id = :p"), {"p": pid_s})

    disparos: list[uuid.UUID] = []

    async def _registrado(**kw):
        disparos.append(kw["pipeline_id"])
        return {"id": str(uuid.uuid4()), "status": "executando"}

    monkeypatch.setattr(rotas_pipelines, "_executar_dag", _registrado)

    lista = await cliente.get("/pipelines", headers=hs)
    assert lista.status_code == 200
    assert {p["id"] for p in lista.json()} == {str(pid_a), str(pid_b), str(pid_s)}
    for pid in (pid_a, pid_b, pid_s):
        r = await cliente.post(f"/pipelines/{pid}/executar-encadeado", headers=hs)
        assert r.status_code == 200, r.text
        r = await cliente.post(f"/pipelines/{pid}/parametros/normalizados", json={}, headers=hs)
        assert r.status_code == 200, r.text
        # The "runs of the day" question used to go through `_autorizar_execucao`, which only
        # accepted specialist/admin, so a service client got a 403 here. The test pipeline
        # declares no parameter, so the question is matched by equality with `{}`.
        r = await cliente.post(
            f"/pipelines/{pid}/execucoes-do-dia", json={"parametros": {}}, headers=hs
        )
        assert r.status_code == 200, r.text
    assert disparos == [pid_a, pid_b, pid_s]

    # A run by ANOTHER actor, of any pipeline, `sistema` included.
    for pid in (pid_b, pid_s):
        ex = await execucao.criar_execucao(pipeline_id=pid, ator_id=admin)
        assert (await cliente.get(f"/execucoes/{ex['execucao_id']}", headers=hs)).status_code == 200
        r = await cliente.get(f"/execucoes?pipeline_id={pid}", headers=hs)
        assert r.status_code == 200 and r.json()["total"] >= 1, r.text
    assert (await cliente.get("/execucoes", headers=hs)).status_code == 403

    # The ceiling is `executar`: edit/administer do not come with it, neither for a regular item
    # nor for `sistema`.
    async with transacao() as conn:
        for pid in (pid_b, pid_s):
            await permissoes.exigir(conn, uid, "pipeline", pid, "executar")
            for nivel in ("editar", "administrar"):
                with pytest.raises(ErroAplicacao):
                    await permissoes.exigir(conn, uid, "pipeline", pid, nivel)
        # The reach is for PIPELINES: other resource types stay under fine-grained permission.
        assert not await permissoes.tem_permissao(conn, uid, "view", uuid.uuid4(), "ver")

    # Authoring: nothing. (With the specialist profile the same account WOULD create; see
    # test_conta_servico.py.)
    assert (
        await cliente.post("/pipelines", json={"nome": "from_account"}, headers=hs)
    ).status_code == 403
    r = await cliente.put(
        f"/pipelines/{pid_a}/rascunho", json={"definicao": DEFINICAO, "base_numero": 1}, headers=hs
    )
    assert r.status_code == 403
    assert (
        await cliente.post(
            "/conexoes", json={"nome": "x", "tipo": "arquivo", "config": {}}, headers=hs
        )
    ).status_code == 403


async def test_so_admin_gere_tokens_e_conta_de_servico_nao_tem_link(owner_engine, cliente):
    _, h = await _admin(owner_engine, cliente)
    _, h_esp = await usuario_logado(
        owner_engine, cliente, email="esp.token@example.com", perfil="especialista"
    )
    uid = await _servico(owner_engine)
    corpo = (await _emitir(cliente, h, uid)).json()
    assert (await _emitir(cliente, h_esp, uid)).status_code == 403
    assert (await cliente.get(f"/usuarios/{uid}/tokens", headers=h_esp)).status_code == 403
    url = f"/usuarios/{uid}/tokens/{corpo['id']}"
    assert (await cliente.delete(url, headers=h_esp)).status_code == 403

    r = await cliente.post(f"/usuarios/{uid}/reenviar-acesso", headers=h)
    assert r.status_code == 409 and r.json()["erro"]["codigo"] == "conta_servico_sem_senha"
    r = await cliente.post(
        "/usuarios",
        json={"email": "svc.nova@example.com", "nome": "New service", "perfil": "servico"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["conta_servico"] is True and r.json()["email_entregue"] is None
    # Correcting a service account's e-mail does not try to send a link (and does not undo the
    # correction).
    r = await cliente.patch(
        f"/usuarios/{r.json()['id']}/email", json={"email": "svc.nova2@example.com"}, headers=h
    )
    assert r.status_code == 200 and r.json()["reenviado"] is False
