"""User CRUD."""

from __future__ import annotations

import pytest

from cflow_core.auth import notificacao
from cflow_core.auth.notificacao import ProvedorMemoria

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


@pytest.fixture
def canal():
    mem = ProvedorMemoria()
    notificacao.definir_provedor(mem)
    yield mem
    notificacao.definir_provedor(None)


async def test_admin_cria_usuario_e_dispara_senha(owner_engine, app_engine, cliente, canal):
    _, h = await usuario_logado(owner_engine, cliente, email="a@example.com", perfil="admin")
    r = await cliente.post(
        "/usuarios",
        json={"email": "novo@example.com", "nome": "Novo", "perfil": "operador"},
        headers=h,
    )
    assert r.status_code == 201
    # the first-password token is delivered through the channel, never in the response
    assert len(canal.enviados) == 1
    assert canal.enviados[0][0] == "novo@example.com"
    assert canal.enviados[0][1] not in r.text


async def test_email_duplicado_409(owner_engine, app_engine, cliente, canal):
    _, h = await usuario_logado(owner_engine, cliente, email="a2@example.com", perfil="admin")
    body = {"email": "dup@example.com", "nome": "X", "perfil": "operador"}
    assert (await cliente.post("/usuarios", json=body, headers=h)).status_code == 201
    assert (await cliente.post("/usuarios", json=body, headers=h)).status_code == 409


async def test_desativar_impede_login_e_reativar_restaura(owner_engine, app_engine, cliente, canal):
    _, h = await usuario_logado(owner_engine, cliente, email="a3@example.com", perfil="admin")
    # create an operator with a known password through the seed helper (direct login)
    from ._arnes_auth import semear_usuario

    SENHA = "long-operator-password-1"
    uid = await semear_usuario(
        owner_engine, email="opx@example.com", senha=SENHA, perfil="operador"
    )

    assert (
        await cliente.post("/auth/login", json={"email": "opx@example.com", "senha": SENHA})
    ).status_code == 200
    # deactivate
    assert (
        await cliente.patch(f"/usuarios/{uid}/ativo", json={"ativo": False}, headers=h)
    ).status_code == 204
    assert (
        await cliente.post("/auth/login", json={"email": "opx@example.com", "senha": SENHA})
    ).status_code == 401
    # reactivate
    assert (
        await cliente.patch(f"/usuarios/{uid}/ativo", json={"ativo": True}, headers=h)
    ).status_code == 204
    assert (
        await cliente.post("/auth/login", json={"email": "opx@example.com", "senha": SENHA})
    ).status_code == 200


class ProvedorQuebrado:
    """Unavailable channel (relay down, recipient refused): raises like smtplib does."""

    def enviar_token_recuperacao(self, email: str, token: str) -> None:
        raise OSError("relay unavailable")


@pytest.fixture
def canal_quebrado():
    notificacao.definir_provedor(ProvedorQuebrado())
    yield
    notificacao.definir_provedor(None)


async def test_falha_do_canal_nao_derruba_a_criacao(
    owner_engine, app_engine, cliente, canal_quebrado
):
    """Sending happens AFTER the commit: if the exception propagated it would become a 500 with the
    user already created, the clear token lost and a retry hitting 409, leaving the user with no
    way to set a password.
    """
    _, h = await usuario_logado(owner_engine, cliente, email="a4@example.com", perfil="admin")
    r = await cliente.post(
        "/usuarios",
        json={"email": "no-channel@example.com", "nome": "No Channel", "perfil": "operador"},
        headers=h,
    )
    assert r.status_code == 201
    assert r.json()["email_entregue"] is False  # the API does not pretend the e-mail went out
    # and the user is standing, listable
    lista = (await cliente.get("/usuarios", headers=h)).json()
    assert any(u["email"] == "no-channel@example.com" for u in lista)


async def test_reenviar_acesso_gera_token_novo_e_usavel(owner_engine, app_engine, cliente, canal):
    _, h = await usuario_logado(owner_engine, cliente, email="a5@example.com", perfil="admin")
    novo = await cliente.post(
        "/usuarios",
        json={"email": "reenv@example.com", "nome": "Reenv", "perfil": "operador"},
        headers=h,
    )
    uid = novo.json()["id"]
    primeiro = canal.enviados[0][1]

    r = await cliente.post(f"/usuarios/{uid}/reenviar-acesso", headers=h)
    assert r.status_code == 200
    assert r.json() == {"email": "reenv@example.com", "email_entregue": True}
    assert len(canal.enviados) == 2
    _, segundo = canal.enviados[1]
    assert segundo != primeiro
    assert segundo not in r.text  # the token never appears in the response

    # the new token really sets the password
    SENHA = "long-resend-password-1"
    assert (
        await cliente.post(
            "/auth/recuperacao/confirmar", json={"token": segundo, "nova_senha": SENHA}
        )
    ).status_code == 204
    assert (
        await cliente.post("/auth/login", json={"email": "reenv@example.com", "senha": SENHA})
    ).status_code == 200


async def test_reenviar_para_suspenso_409(owner_engine, app_engine, cliente, canal):
    _, h = await usuario_logado(owner_engine, cliente, email="a6@example.com", perfil="admin")
    uid = (
        await cliente.post(
            "/usuarios",
            json={"email": "susp@example.com", "nome": "Susp", "perfil": "operador"},
            headers=h,
        )
    ).json()["id"]
    assert (
        await cliente.patch(f"/usuarios/{uid}/ativo", json={"ativo": False}, headers=h)
    ).status_code == 204
    r = await cliente.post(f"/usuarios/{uid}/reenviar-acesso", headers=h)
    assert r.status_code == 409
    assert r.json()["erro"]["codigo"] == "usuario_suspenso"


async def test_corrigir_email_troca_login_reenvia_e_audita(
    owner_engine, app_engine, cliente, canal
):
    """A typo in the domain left the user unreachable, and the platform does not delete users."""
    from sqlalchemy import text

    _, h = await usuario_logado(owner_engine, cliente, email="a7@example.com", perfil="admin")
    uid = (
        await cliente.post(
            "/usuarios",
            json={"email": "alice@exmaple.com", "nome": "Alice", "perfil": "operador"},
            headers=h,
        )
    ).json()["id"]

    r = await cliente.patch(
        f"/usuarios/{uid}/email", json={"email": "Alice@Example.com"}, headers=h
    )
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["email"] == "alice@example.com"  # normalised
    assert corpo["email_anterior"] == "alice@exmaple.com"
    assert corpo["reenviado"] is True and corpo["email_entregue"] is True
    assert canal.enviados[-1][0] == "alice@example.com"  # the link went to the NEW address

    # the old address no longer works: recovery generates no token for it
    antes = len(canal.enviados)
    assert (
        await cliente.post("/auth/recuperacao", json={"email": "alice@exmaple.com"})
    ).status_code == 202
    assert len(canal.enviados) == antes

    # trail: one edit record with the e-mail before/after
    async with app_engine.connect() as conn:
        linhas = (
            await conn.execute(
                text(
                    "SELECT antes, depois FROM auditoria WHERE recurso_tipo='usuario' AND recurso_id=:i"
                    " AND acao='editar'"
                ),
                {"i": uid},
            )
        ).all()
    assert [(a["email"], d["email"]) for a, d in linhas] == [
        ("alice@exmaple.com", "alice@example.com")
    ]


async def test_corrigir_email_para_endereco_em_uso_409(owner_engine, app_engine, cliente, canal):
    _, h = await usuario_logado(owner_engine, cliente, email="a8@example.com", perfil="admin")
    uid = (
        await cliente.post(
            "/usuarios",
            json={"email": "um@example.com", "nome": "Um", "perfil": "operador"},
            headers=h,
        )
    ).json()["id"]
    await cliente.post(
        "/usuarios",
        json={"email": "dois@example.com", "nome": "Dois", "perfil": "operador"},
        headers=h,
    )
    r = await cliente.patch(f"/usuarios/{uid}/email", json={"email": "dois@example.com"}, headers=h)
    assert r.status_code == 409
    assert r.json()["erro"]["codigo"] == "email_em_uso"


async def test_corrigir_email_de_suspenso_corrige_sem_reenviar(
    owner_engine, app_engine, cliente, canal
):
    """Resending requires active access; if the correction depended on the resend, the rollback
    would undo it."""
    from sqlalchemy import text

    _, h = await usuario_logado(owner_engine, cliente, email="a9@example.com", perfil="admin")
    uid = (
        await cliente.post(
            "/usuarios",
            json={"email": "errado@exmaple.com", "nome": "Err", "perfil": "operador"},
            headers=h,
        )
    ).json()["id"]
    assert (
        await cliente.patch(f"/usuarios/{uid}/ativo", json={"ativo": False}, headers=h)
    ).status_code == 204

    antes = len(canal.enviados)
    r = await cliente.patch(
        f"/usuarios/{uid}/email", json={"email": "certo@example.com"}, headers=h
    )
    assert r.status_code == 200
    assert r.json()["reenviado"] is False and r.json()["email_entregue"] is False
    assert len(canal.enviados) == antes  # nothing sent
    # but the correction PERSISTED (it was not rolled back)
    async with app_engine.connect() as conn:
        email = (
            await conn.execute(text("SELECT email FROM usuarios WHERE id=:i"), {"i": uid})
        ).scalar_one()
    assert email == "certo@example.com"


async def test_reenviar_e_corrigir_email_de_nao_operador_recusa_especialista(
    owner_engine, app_engine, cliente, canal
):
    """The specialist manages OPERATORS and nothing else: over an auditor both actions are 403.

    This test once required admin for both actions on any target; the reach changed (the
    specialist now creates and manages operators), the limit did not, and the limit is what it
    guards.
    """
    _, ha = await usuario_logado(owner_engine, cliente, email="a10@example.com", perfil="admin")
    uid = (
        await cliente.post(
            "/usuarios",
            json={"email": "alvo@example.com", "nome": "Alvo", "perfil": "auditor"},
            headers=ha,
        )
    ).json()["id"]
    _, he = await usuario_logado(
        owner_engine, cliente, email="esp@example.com", perfil="especialista"
    )

    assert (await cliente.post(f"/usuarios/{uid}/reenviar-acesso", headers=he)).status_code == 403
    assert (
        await cliente.patch(f"/usuarios/{uid}/email", json={"email": "x@example.com"}, headers=he)
    ).status_code == 403


async def test_nao_existe_exclusao_fisica(app_engine, cliente):
    # no DELETE /usuarios/{id} route
    from fastapi.routing import APIRoute

    from cflow_api.main import app

    for rota in app.routes:
        if (
            isinstance(rota, APIRoute)
            and rota.path.startswith("/usuarios")
            and "{usuario_id}" in rota.path
        ):
            assert "DELETE" not in rota.methods
