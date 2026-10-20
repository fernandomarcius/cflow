"""Duplicate a connection: a new connection with the SAME credential, without the secret travelling.

Invariants checked:
  - the encrypted row is COPIED inside the vault (same bytes, new reference), never decrypted in
    the API;
  - the copy works (testing resolves the secret) and is independent: rotating or deleting the
    source does not touch it;
  - it is born WITHOUT write permission, even when the source has it (enabling is a separate admin
    action);
  - trail: `criar` for the new one, with `duplicada_de` and no secret;
  - edge: creator or admin of the SOURCE (another specialist -> 403); empty name 400, name in use
    409.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ._arnes_auth import CONFIG_PG, SENHA_APP, usuario_logado

pytestmark = pytest.mark.integration

# The test database's own cflow_app role: a valid login (testing returns ok).
SENHA_BOA = SENHA_APP
CONFIG = CONFIG_PG


async def _criar(cliente, h, nome, segredo=SENHA_BOA) -> str:
    r = await cliente.post(
        "/conexoes",
        json={"nome": nome, "tipo": "postgres", "config": CONFIG, "segredo": segredo},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _linha(owner_engine, cid):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT nome, tipo, config, credencial_ref, permitir_escrita, criado_por "
                    "FROM conexoes WHERE id = CAST(:c AS uuid)"
                ),
                {"c": cid},
            )
        ).first()


async def _cifrado(owner_engine, ref):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT segredo_cifrado FROM cofre_credenciais WHERE id = CAST(:r AS uuid)"),
                {"r": str(ref)},
            )
        ).scalar_one_or_none()


async def _duplicar(cliente, h, cid, nome):
    return await cliente.post(f"/conexoes/{cid}/duplicar", json={"nome": nome}, headers=h)


async def test_copia_a_credencial_sem_decifrar_e_nasce_sem_escrita(owner_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="cxdup1@example.com", perfil="admin")
    cid = await _criar(cliente, h, "dup-source")
    r = await cliente.patch(f"/conexoes/{cid}/escrita", json={"permitir_escrita": True}, headers=h)
    assert r.status_code == 200, r.text

    r = await _duplicar(cliente, h, cid, "  dup-copy  ")
    assert r.status_code == 201, r.text
    nid = r.json()["id"]
    assert SENHA_BOA not in r.text

    origem, copia = await _linha(owner_engine, cid), await _linha(owner_engine, nid)
    assert copia.nome == "dup-copy"
    assert (copia.tipo, copia.config) == (origem.tipo, origem.config)
    assert origem.permitir_escrita is True and copia.permitir_escrita is False
    assert copia.credencial_ref and copia.credencial_ref != origem.credencial_ref
    # Same bytes: copied inside the vault. Re-encrypting would yield another Fernet token (random IV).
    assert bytes(await _cifrado(owner_engine, copia.credencial_ref)) == bytes(
        await _cifrado(owner_engine, origem.credencial_ref)
    )

    t = await cliente.post(f"/conexoes/{nid}/testar", headers=h)
    assert t.status_code == 200 and t.json()["ok"] is True, t.text

    lista = await cliente.get("/conexoes", headers=h)
    assert SENHA_BOA not in lista.text and "credencial_ref" not in lista.text

    async with owner_engine.connect() as conn:
        trilha = (
            await conn.execute(
                text(
                    "SELECT acao, depois::text AS depois FROM auditoria "
                    "WHERE recurso_tipo = 'conexao' AND recurso_id = CAST(:r AS uuid)"
                ),
                {"r": nid},
            )
        ).all()
    assert len(trilha) == 1
    assert trilha[0].acao == "criar"
    assert cid in trilha[0].depois and "duplicada_de" in trilha[0].depois
    assert SENHA_BOA not in trilha[0].depois


async def test_origem_e_copia_sao_independentes(owner_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="cxdup2@example.com", perfil="admin")
    cid = await _criar(cliente, h, "dup-indep")
    nid = (await _duplicar(cliente, h, cid, "dup-indep-copy")).json()["id"]
    ref_copia = (await _linha(owner_engine, nid)).credencial_ref
    cifrado_copia = bytes(await _cifrado(owner_engine, ref_copia))

    r = await cliente.post(
        f"/conexoes/{cid}/rotacionar-credencial", json={"novo_segredo": "outra-senha"}, headers=h
    )
    assert r.status_code == 204, r.text
    assert bytes(await _cifrado(owner_engine, ref_copia)) == cifrado_copia

    assert (await cliente.delete(f"/conexoes/{cid}", headers=h)).status_code == 204
    assert await _cifrado(owner_engine, ref_copia) is not None
    t = await cliente.post(f"/conexoes/{nid}/testar", headers=h)
    assert t.json()["ok"] is True, t.text


async def test_so_criador_ou_admin_da_origem_duplica(owner_engine, cliente):
    _, h_dono = await usuario_logado(
        owner_engine, cliente, email="cxdup3@example.com", perfil="especialista"
    )
    _, h_outro = await usuario_logado(
        owner_engine, cliente, email="cxdup4@example.com", perfil="especialista"
    )
    _, h_op = await usuario_logado(
        owner_engine, cliente, email="cxdup5@example.com", perfil="operador"
    )
    cid = await _criar(cliente, h_dono, "dup-edge")
    assert (await _duplicar(cliente, h_outro, cid, "dup-edge-other")).status_code == 403
    assert (await _duplicar(cliente, h_op, cid, "dup-edge-op")).status_code == 403
    r = await _duplicar(cliente, h_dono, cid, "dup-edge-owner")
    assert r.status_code == 201, r.text
    assert (await _linha(owner_engine, r.json()["id"])).criado_por is not None


async def test_nome_vazio_ou_em_uso_e_recusado_sem_criar_nada(owner_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="cxdup6@example.com", perfil="admin")
    cid = await _criar(cliente, h, "dup-name")
    await _criar(cliente, h, "dup-name-taken")
    r = await _duplicar(cliente, h, cid, "   ")
    assert r.status_code == 400 and r.json()["erro"]["codigo"] == "nome_invalido"
    r = await _duplicar(cliente, h, cid, "dup-name-taken")
    assert r.status_code == 409 and r.json()["erro"]["codigo"] == "nome_em_uso"
    async with owner_engine.connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM conexoes"))).scalar_one()
        cofre = (await conn.execute(text("SELECT count(*) FROM cofre_credenciais"))).scalar_one()
    assert (n, cofre) == (2, 2)


async def test_conexao_sem_credencial_duplica_sem_credencial(owner_engine, cliente):
    _, h = await usuario_logado(owner_engine, cliente, email="cxdup7@example.com", perfil="admin")
    r = await cliente.post(
        "/conexoes",
        json={"nome": "dup-file", "tipo": "arquivo", "config": {"caminho": "/tmp/x.csv"}},
        headers=h,
    )
    assert r.status_code == 201, r.text
    nid = (await _duplicar(cliente, h, r.json()["id"], "dup-file-copy")).json()["id"]
    assert (await _linha(owner_engine, nid)).credencial_ref is None
