"""Every authentication event is audited."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from cflow_core.auth import notificacao
from cflow_core.auth.notificacao import ProvedorMemoria

from ._arnes_auth import criar_sessao, semear_usuario

pytestmark = pytest.mark.integration

SENHA = "long-audited-password-3"


@pytest.fixture
def canal():
    mem = ProvedorMemoria()
    notificacao.definir_provedor(mem)
    yield mem
    notificacao.definir_provedor(None)


async def _acoes(app_engine) -> set[str]:
    async with app_engine.connect() as conn:
        linhas = (await conn.execute(text("SELECT acao FROM auditoria"))).scalars().all()
    return set(linhas)


async def test_eventos_de_auth_auditados(owner_engine, app_engine, cliente, canal):
    uid = await semear_usuario(owner_engine, email="aud@example.com", senha=SENHA)

    # successful login
    await cliente.post("/auth/login", json={"email": "aud@example.com", "senha": SENHA})
    # failed login
    await cliente.post(
        "/auth/login", json={"email": "aud@example.com", "senha": "wrong-long-password-1"}
    )
    # logout (uses a direct session)
    token = await criar_sessao(app_engine, uid)
    await cliente.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
    # recovery + password change
    await cliente.post("/auth/recuperacao", json={"email": "aud@example.com"})
    _, rtoken = canal.enviados[0]
    await cliente.post(
        "/auth/recuperacao/confirmar", json={"token": rtoken, "nova_senha": "new-long-password-4"}
    )

    acoes = await _acoes(app_engine)
    assert {"login", "login_falha", "logout", "recuperacao_iniciada", "senha_trocada"} <= acoes


async def test_login_grava_ultimo_acesso(owner_engine, app_engine, cliente):
    """`ultimo_acesso` exists in the table and is returned by the API, so login must write it,
    otherwise the users screen shows "awaiting first access" even for someone logged in."""
    from sqlalchemy import text

    from ._arnes_auth import login_token, semear_usuario

    await semear_usuario(
        owner_engine,
        email="acesso@example.com",
        senha="access-long-password",
        perfil="admin",
    )
    async with app_engine.connect() as conn:
        antes = (
            await conn.execute(
                text("SELECT ultimo_acesso FROM usuarios WHERE email = 'acesso@example.com'")
            )
        ).scalar_one()
    assert antes is None

    token = await login_token(cliente, "acesso@example.com", "access-long-password")
    async with app_engine.connect() as conn:
        depois = (
            await conn.execute(
                text("SELECT ultimo_acesso FROM usuarios WHERE email = 'acesso@example.com'")
            )
        ).scalar_one()
    assert depois is not None

    # And the listing returns the value (it is what the UI reads).
    r = await cliente.get("/usuarios", headers={"Authorization": f"Bearer {token}"})
    alvo = next(u for u in r.json() if u["email"] == "acesso@example.com")
    assert alvo["ultimo_acesso"] is not None
