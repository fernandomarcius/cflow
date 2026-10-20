"""Connection registration and listing; the secret is never exposed."""

from __future__ import annotations

import pytest

from ._arnes_auth import CONFIG_PG, SENHA_APP, usuario_logado

pytestmark = pytest.mark.integration

SEGREDO = SENHA_APP
CONFIG = CONFIG_PG


async def test_criar_e_listar_sem_segredo(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esp@example.com", perfil="especialista"
    )
    r = await cliente.post(
        "/conexoes",
        json={"nome": "pg-teste", "tipo": "postgres", "config": CONFIG, "segredo": SEGREDO},
        headers=h,
    )
    assert r.status_code == 201

    lst = await cliente.get("/conexoes", headers=h)
    assert lst.status_code == 200
    assert SEGREDO not in lst.text  # the secret is never in the response
    nomes = [c["nome"] for c in lst.json()]
    assert "pg-teste" in nomes
    # the exposed config has no secret
    cfg = next(c["config"] for c in lst.json() if c["nome"] == "pg-teste")
    assert "senha" not in cfg and "segredo" not in cfg


async def test_config_com_segredo_e_rejeitada(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="esp2@example.com", perfil="especialista"
    )
    r = await cliente.post(
        "/conexoes",
        json={"nome": "ruim", "tipo": "postgres", "config": {"host": "x", "senha": "vazou"}},
        headers=h,
    )
    assert r.status_code == 400


async def test_excluir_conexao_remove_cofre_e_audita(owner_engine, app_engine, cliente):
    from sqlalchemy import text

    _, h = await usuario_logado(
        owner_engine, cliente, email="delcx@example.com", perfil="especialista"
    )
    cid = (
        await cliente.post(
            "/conexoes",
            json={"nome": "to-delete", "tipo": "postgres", "config": CONFIG, "segredo": SEGREDO},
            headers=h,
        )
    ).json()["id"]
    async with owner_engine.connect() as conn:
        cofre_antes = (
            await conn.execute(text("SELECT count(*) FROM cofre_credenciais"))
        ).scalar_one()
    assert cofre_antes == 1  # credential stored

    assert (await cliente.delete(f"/conexoes/{cid}", headers=h)).status_code == 204
    assert cid not in [c["id"] for c in (await cliente.get("/conexoes", headers=h)).json()]
    async with owner_engine.connect() as conn:
        n_cx = (
            await conn.execute(text("SELECT count(*) FROM conexoes WHERE id = :r"), {"r": cid})
        ).scalar_one()
        n_cofre = (await conn.execute(text("SELECT count(*) FROM cofre_credenciais"))).scalar_one()
        n_aud = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM auditoria WHERE acao='excluir' AND recurso_tipo='conexao' AND recurso_id = :r"
                ),
                {"r": cid},
            )
        ).scalar_one()
    assert n_cx == 0 and n_cofre == 0 and n_aud == 1  # connection AND vault credential deleted


async def test_excluir_conexao_so_criador_ou_admin(owner_engine, app_engine, cliente):
    # the creator creates; another specialist (not the creator) -> 403; an admin can delete
    _, dono = await usuario_logado(
        owner_engine, cliente, email="cxdono@example.com", perfil="especialista"
    )
    cid = (
        await cliente.post(
            "/conexoes",
            json={"nome": "owners-conn", "tipo": "postgres", "config": CONFIG},
            headers=dono,
        )
    ).json()["id"]

    _, outro = await usuario_logado(
        owner_engine, cliente, email="cxoutro@example.com", perfil="especialista"
    )
    assert (await cliente.delete(f"/conexoes/{cid}", headers=outro)).status_code == 403

    _, adm = await usuario_logado(owner_engine, cliente, email="cxadm@example.com", perfil="admin")
    assert (await cliente.delete(f"/conexoes/{cid}", headers=adm)).status_code == 204
