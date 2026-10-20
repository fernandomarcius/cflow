"""The user's own account: /auth/me and self-service password change (/auth/senha)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

SENHA = "current-long-password-1"
NOVA = "new-long-password-2"


async def test_me_devolve_dados_do_usuario(owner_engine, cliente):
    _, headers = await usuario_logado(
        owner_engine, cliente, email="me@example.com", perfil="especialista", senha=SENHA
    )
    r = await cliente.get("/auth/me", headers=headers)
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["email"] == "me@example.com"
    assert corpo["perfil"] == "especialista"
    assert corpo["nome"]
    assert "senha_hash" not in corpo and "senha" not in corpo  # no secret leaks


async def test_me_exige_autenticacao(cliente):
    r = await cliente.get("/auth/me")
    assert r.status_code in (401, 403)


async def test_troca_senha_feliz(owner_engine, app_engine, cliente):
    _, headers = await usuario_logado(
        owner_engine, cliente, email="troca@example.com", perfil="operador", senha=SENHA
    )
    r = await cliente.post(
        "/auth/senha", json={"senha_atual": SENHA, "nova_senha": NOVA}, headers=headers
    )
    assert r.status_code == 204, r.text

    # the new password logs in; the old one fails
    assert (
        await cliente.post("/auth/login", json={"email": "troca@example.com", "senha": NOVA})
    ).status_code == 200
    assert (
        await cliente.post("/auth/login", json={"email": "troca@example.com", "senha": SENHA})
    ).status_code == 401

    # trail: the change was audited (no secret in the diff)
    async with app_engine.connect() as conn:
        linhas = (
            await conn.execute(
                text("SELECT acao, antes, depois FROM auditoria WHERE acao = 'senha_trocada'")
            )
        ).all()
    assert len(linhas) == 1
    blob = str(linhas[0]._mapping)
    assert SENHA not in blob and NOVA not in blob


async def test_senha_atual_errada_recusa(owner_engine, cliente):
    _, headers = await usuario_logado(
        owner_engine, cliente, email="errada@example.com", perfil="operador", senha=SENHA
    )
    r = await cliente.post(
        "/auth/senha",
        json={"senha_atual": "wrong-guess-long-enough-9", "nova_senha": NOVA},
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["erro"]["codigo"] == "senha_atual_invalida"
    # the password did NOT change: the current one still logs in
    assert (
        await cliente.post("/auth/login", json={"email": "errada@example.com", "senha": SENHA})
    ).status_code == 200


async def test_politica_minima_aplicada(owner_engine, cliente):
    _, headers = await usuario_logado(
        owner_engine, cliente, email="curta@example.com", perfil="operador", senha=SENHA
    )
    r = await cliente.post(
        "/auth/senha", json={"senha_atual": SENHA, "nova_senha": "curta"}, headers=headers
    )
    assert r.status_code == 400
    assert r.json()["erro"]["codigo"] == "senha_fraca"
