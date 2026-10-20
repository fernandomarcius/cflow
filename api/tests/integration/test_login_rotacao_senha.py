"""A login IN FLIGHT during a password change.

`scripts/conta_servico.py --reset-password` swaps the hash and revokes the sessions in the same
transaction. That covers the sessions that ALREADY exist, but not a login that verified the old
password before the change committed: its session was inserted after the script's `UPDATE
sessoes` (or waited for the row lock and was written afterwards), and the Bearer obtained with the
old password stayed valid for up to 24 h, while the trail said "sessions revoked".

The route now ties the session to the hash it verified: `UPDATE usuarios ... WHERE id = :id AND
senha_hash = :hash_lido`. Both directions of the race are proven here, with DETERMINISTIC
interleaving (a hook on a call the route already makes), without changing anything in the script.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys

import pytest
from sqlalchemy import text

from cflow_core.auth import forca_bruta, mfa_fluxo, notificacao

from ._arnes_auth import semear_usuario
from ._plataforma import RAIZ_PLATAFORMA as RAIZ

pytestmark = pytest.mark.integration


EMAIL = "svc.rotacao@example.com"
ADMIN = "admin.rotacao@example.com"
ANTIGA = "old-service-account-password"
NOVA = "new-service-account-password-2"


def _carregar():
    """The script by its REAL path (same reason as `test_conta_servico.py`: copying the snippet
    here would test a copy)."""
    caminho = RAIZ / "scripts" / "conta_servico.py"
    spec = importlib.util.spec_from_file_location("conta_servico_rotacao", caminho)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["conta_servico_rotacao"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
async def script(owner_engine, monkeypatch):
    # In-memory channel: the default test provider would print the password-setting token.
    monkeypatch.setattr(notificacao, "_provedor", notificacao.ProvedorMemoria())
    mod = _carregar()
    await semear_usuario(
        owner_engine, email=ADMIN, senha="long-admin-password-value", perfil="admin"
    )
    await mod.aplicar(email=EMAIL, nome="Service account", senha=ANTIGA, ator=ADMIN)
    return mod


async def _redefinir(script) -> list[str]:
    return await script.aplicar(
        email=EMAIL, nome="Service account", senha=NOVA, redefinir_senha=True, ator=ADMIN
    )


async def _sessoes_vivas(owner_engine) -> int:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT count(*) FROM sessoes s JOIN usuarios u ON u.id = s.usuario_id "
                    "WHERE u.email = :e AND s.revogada_em IS NULL"
                ),
                {"e": EMAIL},
            )
        ).scalar_one()


async def _me(cliente, token: str) -> int:
    r = await cliente.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    return r.status_code


async def test_troca_de_senha_que_commita_no_meio_do_login_recusa_o_login_em_voo(
    script, owner_engine, cliente, monkeypatch
):
    """The login verified the OLD password and paused (hook on `exige_mfa`, the first async call
    after `verificar`); the change commits entirely in that window. On resuming, the verified
    hash is no longer the row's: generic 401 and NO live session (before, 200 with a Bearer that
    survived the rotation)."""
    lido, liberar = asyncio.Event(), asyncio.Event()
    original = mfa_fluxo.exige_mfa

    async def _pausa(conn, usuario_id):
        if not liberar.is_set():
            lido.set()
            await liberar.wait()
        return await original(conn, usuario_id)

    monkeypatch.setattr(mfa_fluxo, "exige_mfa", _pausa)
    em_voo = asyncio.create_task(
        cliente.post("/auth/login", json={"email": EMAIL, "senha": ANTIGA})
    )
    await asyncio.wait_for(lido.wait(), 10)
    # Does not block: the in-flight login has not touched the `usuarios` row yet.
    await asyncio.wait_for(_redefinir(script), 20)
    liberar.set()
    r = await asyncio.wait_for(em_voo, 20)

    assert r.status_code == 401, r.text
    assert r.json()["erro"]["codigo"] == "credenciais_invalidas"
    assert await _sessoes_vivas(owner_engine) == 0
    nova = await cliente.post("/auth/login", json={"email": EMAIL, "senha": NOVA})
    assert nova.status_code == 200, nova.text


async def test_login_que_travou_a_linha_antes_da_troca_tem_a_sessao_revogada_por_ela(
    script, owner_engine, cliente, monkeypatch
):
    """The opposite direction: the login already went through `UPDATE ... WHERE senha_hash =
    :hash_lido` (the row stays locked until its commit) and paused before creating the session.
    The change, fired now, WAITS for the lock: it only proceeds after the session exists, and
    then revokes it."""
    parado, liberar = asyncio.Event(), asyncio.Event()
    original = forca_bruta.registrar

    async def _pausa(conn, *, email, ip, sucesso):
        if email == EMAIL and sucesso and not liberar.is_set():
            parado.set()
            await liberar.wait()
        return await original(conn, email=email, ip=ip, sucesso=sucesso)

    monkeypatch.setattr(forca_bruta, "registrar", _pausa)
    em_voo = asyncio.create_task(
        cliente.post("/auth/login", json={"email": EMAIL, "senha": ANTIGA})
    )
    await asyncio.wait_for(parado.wait(), 10)
    troca = asyncio.create_task(_redefinir(script))
    await asyncio.sleep(1.0)
    assert not troca.done(), "the change must wait for the lock the in-flight login holds"
    liberar.set()
    r = await asyncio.wait_for(em_voo, 20)
    await asyncio.wait_for(troca, 20)

    assert r.status_code == 200, r.text
    assert await _me(cliente, r.json()["token"]) == 401
    assert await _sessoes_vivas(owner_engine) == 0


async def test_sem_troca_o_login_continua_normal(script, owner_engine, cliente):
    r = await cliente.post("/auth/login", json={"email": EMAIL, "senha": ANTIGA})
    assert r.status_code == 200, r.text
    assert await _me(cliente, r.json()["token"]) == 200
    async with owner_engine.connect() as conn:
        acesso = (
            await conn.execute(
                text("SELECT ultimo_acesso FROM usuarios WHERE email = :e"), {"e": EMAIL}
            )
        ).scalar_one()
    assert acesso is not None
