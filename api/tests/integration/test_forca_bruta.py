"""Brute-force protection."""

from __future__ import annotations

import pytest

from ._arnes_auth import semear_usuario

pytestmark = pytest.mark.integration

SENHA = "long-enough-password-123"


async def test_bloqueia_apos_excesso_de_falhas(owner_engine, app_engine, cliente):
    await semear_usuario(owner_engine, email="alvo@example.com", senha=SENHA)

    # 5 failures (default limit) -> the following attempts are blocked (429)
    for _ in range(5):
        r = await cliente.post(
            "/auth/login", json={"email": "alvo@example.com", "senha": "wrong-but-long-1"}
        )
        assert r.status_code == 401

    bloqueado = await cliente.post(
        "/auth/login", json={"email": "alvo@example.com", "senha": SENHA}
    )
    assert bloqueado.status_code == 429
    assert bloqueado.json()["erro"]["codigo"] == "muitas_tentativas"
