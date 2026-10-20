"""TOTP MFA: configure -> activate -> second factor required at login."""

from __future__ import annotations

import pytest

from cflow_core.auth.mfa import totp_atual

from ._arnes_auth import login_token, semear_usuario

pytestmark = pytest.mark.integration

SENHA = "long-mfa-account-password"


async def test_fluxo_mfa_completo(owner_engine, app_engine, cliente):
    await semear_usuario(owner_engine, email="mfa@example.com", senha=SENHA, perfil="admin")
    token = await login_token(cliente, "mfa@example.com", SENHA)
    h = {"Authorization": f"Bearer {token}"}

    cfg = await cliente.post("/auth/mfa/configurar", headers=h)
    assert cfg.status_code == 200
    segredo = cfg.json()["segredo"]
    assert cfg.json()["uri"].startswith("otpauth://totp/")

    # activate with a correct TOTP
    assert (
        await cliente.post("/auth/mfa/ativar", json={"codigo": totp_atual(segredo)}, headers=h)
    ).status_code == 204

    # login with the password only -> requires the 2nd factor
    r = await cliente.post("/auth/login", json={"email": "mfa@example.com", "senha": SENHA})
    assert r.status_code == 401 and r.json()["erro"]["codigo"] == "mfa_requerido"

    # login with password + correct TOTP -> ok
    ok = await cliente.post(
        "/auth/login",
        json={"email": "mfa@example.com", "senha": SENHA, "codigo": totp_atual(segredo)},
    )
    assert ok.status_code == 200 and ok.json()["token"]

    # login with a wrong TOTP -> refused
    ruim = await cliente.post(
        "/auth/login", json={"email": "mfa@example.com", "senha": SENHA, "codigo": "000000"}
    )
    assert ruim.status_code == 401


async def test_ativar_codigo_errado(owner_engine, app_engine, cliente):
    await semear_usuario(owner_engine, email="mfa2@example.com", senha=SENHA)
    token = await login_token(cliente, "mfa2@example.com", SENHA)
    h = {"Authorization": f"Bearer {token}"}
    await cliente.post("/auth/mfa/configurar", headers=h)
    r = await cliente.post("/auth/mfa/ativar", json={"codigo": "000000"}, headers=h)
    assert r.status_code == 400 and r.json()["erro"]["codigo"] == "mfa_invalido"


async def test_me_expoe_mfa_habilitado_e_desativar_exige_senha(owner_engine, app_engine, cliente):
    """The account page needs to know whether the 2nd factor is on (otherwise it would only offer
    'configure', even to someone who already did) and turning it off requires the current
    password."""
    await semear_usuario(owner_engine, email="mfa3@example.com", senha=SENHA)
    token = await login_token(cliente, "mfa3@example.com", SENHA)
    h = {"Authorization": f"Bearer {token}"}

    assert (await cliente.get("/auth/me", headers=h)).json()["mfa_habilitado"] is False

    segredo = (await cliente.post("/auth/mfa/configurar", headers=h)).json()["segredo"]
    # Configuring does not turn it on: only confirming the code does.
    assert (await cliente.get("/auth/me", headers=h)).json()["mfa_habilitado"] is False
    await cliente.post("/auth/mfa/ativar", json={"codigo": totp_atual(segredo)}, headers=h)
    assert (await cliente.get("/auth/me", headers=h)).json()["mfa_habilitado"] is True

    # A wrong password does not turn off the 2nd factor (a forgotten open session does not drop
    # the protection).
    ruim = await cliente.post(
        "/auth/mfa/desativar", json={"senha_atual": "some-other-password"}, headers=h
    )
    assert ruim.status_code == 400 and ruim.json()["erro"]["codigo"] == "senha_atual_invalida"
    assert (await cliente.get("/auth/me", headers=h)).json()["mfa_habilitado"] is True

    ok = await cliente.post("/auth/mfa/desativar", json={"senha_atual": SENHA}, headers=h)
    assert ok.status_code == 204
    assert (await cliente.get("/auth/me", headers=h)).json()["mfa_habilitado"] is False
    # Switched off, login passes with the password alone again.
    assert (
        await cliente.post("/auth/login", json={"email": "mfa3@example.com", "senha": SENHA})
    ).status_code == 200


async def test_desativar_mfa_grava_na_trilha(owner_engine, app_engine, cliente):
    await semear_usuario(owner_engine, email="mfa4@example.com", senha=SENHA)
    token = await login_token(cliente, "mfa4@example.com", SENHA)
    h = {"Authorization": f"Bearer {token}"}
    segredo = (await cliente.post("/auth/mfa/configurar", headers=h)).json()["segredo"]
    await cliente.post("/auth/mfa/ativar", json={"codigo": totp_atual(segredo)}, headers=h)
    await cliente.post("/auth/mfa/desativar", json={"senha_atual": SENHA}, headers=h)

    from sqlalchemy import text

    async with app_engine.connect() as conn:
        linhas = (
            await conn.execute(
                text(
                    "SELECT antes, depois FROM auditoria WHERE recurso_tipo = 'mfa' "
                    "ORDER BY ocorrido_em"
                )
            )
        ).all()
    # Two rows: activation (False->True) and deactivation (True->False). Neither carries the secret.
    assert [(linha.antes["mfa"], linha.depois["mfa"]) for linha in linhas] == [
        (False, True),
        (True, False),
    ]
    assert "segredo" not in str(linhas)
