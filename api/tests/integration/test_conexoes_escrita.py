"""Per-connection `permitir_escrita` flag (enables effect nodes).

Invariants:
  - PATCH /conexoes/{id}/escrita is admin-only (non-admin -> generic 403);
  - admin turns it on/off; the change is written to the trail (same transaction) with a
    before/after diff;
  - a no-op (same value) does not create a new audit record;
  - listing exposes `permitir_escrita`; no secret anywhere.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

CONFIG = {"host": "localhost", "porta": 5433, "base": "cflow", "usuario": "cflow_app"}
SENHA_BOA = "change-in-production-app"


async def _criar(cliente, h, nome):
    r = await cliente.post(
        "/conexoes",
        json={"nome": nome, "tipo": "postgres", "config": CONFIG, "segredo": SENHA_BOA},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _audit_count(owner_engine, cid: str) -> int:
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT count(*) FROM auditoria WHERE recurso_tipo = 'conexao' "
                    "AND recurso_id = CAST(:i AS uuid) AND acao = 'editar'"
                ),
                {"i": cid},
            )
        ).scalar_one()


async def test_admin_liga_e_lista_expoe_flag(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="cxesc_adm@example.com", perfil="admin"
    )
    cid = await _criar(cliente, h, "esc-liga")

    # a new connection starts with the flag off
    lst = (await cliente.get("/conexoes", headers=h)).json()
    alvo = next(c for c in lst if c["id"] == cid)
    assert alvo["permitir_escrita"] is False

    r = await cliente.patch(f"/conexoes/{cid}/escrita", json={"permitir_escrita": True}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"id": cid, "permitir_escrita": True}

    lst = (await cliente.get("/conexoes", headers=h)).json()
    alvo = next(c for c in lst if c["id"] == cid)
    assert alvo["permitir_escrita"] is True
    # the listing NEVER returns credential/secret
    assert "credencial_ref" not in alvo and "segredo" not in alvo


async def test_admin_desliga(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="cxesc_adm2@example.com", perfil="admin"
    )
    cid = await _criar(cliente, h, "esc-desliga")
    await cliente.patch(f"/conexoes/{cid}/escrita", json={"permitir_escrita": True}, headers=h)
    r = await cliente.patch(f"/conexoes/{cid}/escrita", json={"permitir_escrita": False}, headers=h)
    assert r.status_code == 200
    assert r.json()["permitir_escrita"] is False


async def test_nao_admin_403(owner_engine, app_engine, cliente):
    # create as admin, try to turn on as especialista -> generic 403.
    _, adm = await usuario_logado(
        owner_engine, cliente, email="cxesc_adm3@example.com", perfil="admin"
    )
    cid = await _criar(cliente, adm, "esc-403")
    _, esp = await usuario_logado(
        owner_engine, cliente, email="cxesc_esp@example.com", perfil="especialista"
    )
    r = await cliente.patch(
        f"/conexoes/{cid}/escrita", json={"permitir_escrita": True}, headers=esp
    )
    assert r.status_code == 403
    # the body reveals neither the resource existence nor any detail
    assert "permitir_escrita" not in r.text


async def test_auditoria_diff_mesma_transacao(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="cxesc_aud@example.com", perfil="admin"
    )
    cid = await _criar(cliente, h, "esc-audit")
    await cliente.patch(f"/conexoes/{cid}/escrita", json={"permitir_escrita": True}, headers=h)

    async with owner_engine.begin() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT acao, antes, depois FROM auditoria "
                        "WHERE recurso_tipo = 'conexao' AND recurso_id = CAST(:i AS uuid) "
                        "AND acao = 'editar' ORDER BY id DESC LIMIT 1"
                    ),
                    {"i": cid},
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    assert row["antes"] == {"permitir_escrita": False}
    assert row["depois"] == {"permitir_escrita": True}


async def test_no_op_nao_gera_auditoria(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="cxesc_noop@example.com", perfil="admin"
    )
    cid = await _criar(cliente, h, "esc-noop")
    await cliente.patch(f"/conexoes/{cid}/escrita", json={"permitir_escrita": True}, headers=h)
    n1 = await _audit_count(owner_engine, cid)
    # second PATCH with the SAME value: no-op -> no new record
    r = await cliente.patch(f"/conexoes/{cid}/escrita", json={"permitir_escrita": True}, headers=h)
    assert r.status_code == 200
    assert await _audit_count(owner_engine, cid) == n1


async def test_conexao_inexistente_404(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="cxesc_404@example.com", perfil="admin"
    )
    r = await cliente.patch(
        "/conexoes/00000000-0000-0000-0000-000000000000/escrita",
        json={"permitir_escrita": True},
        headers=h,
    )
    assert r.status_code == 404
