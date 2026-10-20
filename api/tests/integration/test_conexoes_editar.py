"""Connection: rename (PATCH) and edit (PUT); config without a secret, credential kept/rotated.

Invariants checked:
  - editing persists the new `config`; the secret NEVER appears in config/response/audit diff;
  - credential omitted -> kept (same `credencial_ref`); given -> rotated (ref preserved, encrypted
    value replaced in the vault); with no previous ref -> a new one is stored and pointed to;
  - testing works again with the rotated credential (resolves the NEW secret);
  - authorization: only the creator or an admin can edit/rename (another specialist -> 403);
  - renaming to a name already in use -> 409.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ._arnes_auth import CONFIG_PG, SENHA_APP, usuario_logado

pytestmark = pytest.mark.integration

# The test database's own cflow_app role: a valid login (testing returns ok).
SENHA_BOA = SENHA_APP
CONFIG = CONFIG_PG


async def _criar(cliente, h, nome, segredo=SENHA_BOA):
    return (
        await cliente.post(
            "/conexoes",
            json={"nome": nome, "tipo": "postgres", "config": CONFIG, "segredo": segredo},
            headers=h,
        )
    ).json()["id"]


async def _cred_ref(owner_engine, cid):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT credencial_ref FROM conexoes WHERE id = :r"), {"r": cid}
            )
        ).scalar_one()


async def _cifrado(owner_engine, ref):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT segredo_cifrado FROM cofre_credenciais WHERE id = CAST(:r AS uuid)"),
                {"r": ref},
            )
        ).scalar_one()


async def test_editar_config_mantem_credencial(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="cxed1@example.com", perfil="especialista"
    )
    cid = await _criar(cliente, h, "edit-mantem")
    ref_antes = await _cred_ref(owner_engine, cid)
    cif_antes = await _cifrado(owner_engine, ref_antes)

    nova_cfg = {**CONFIG, "schema_padrao": "public"}
    # credential OMITTED -> kept
    r = await cliente.put(f"/conexoes/{cid}", json={"config": nova_cfg}, headers=h)
    assert r.status_code == 204, r.text

    # config updated and WITHOUT a secret
    cfg = next(
        c["config"] for c in (await cliente.get("/conexoes", headers=h)).json() if c["id"] == cid
    )
    assert cfg["schema_padrao"] == "public"
    assert "senha" not in cfg and "segredo" not in cfg and "password" not in cfg

    # same credential (ref and ciphertext unchanged) -> testing still ok
    ref_depois = await _cred_ref(owner_engine, cid)
    assert ref_depois == ref_antes
    assert await _cifrado(owner_engine, ref_depois) == cif_antes
    t = await cliente.post(f"/conexoes/{cid}/testar", headers=h)
    assert t.status_code == 200 and t.json()["ok"] is True


async def test_editar_rotaciona_credencial(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="cxed2@example.com", perfil="especialista"
    )
    # starts with a WRONG password -> testing gives ok=False
    cid = await _criar(cliente, h, "edit-rotaciona", segredo="wrong-password")
    ref_antes = await _cred_ref(owner_engine, cid)
    assert (await cliente.post(f"/conexoes/{cid}/testar", headers=h)).json()["ok"] is False

    # edit giving the CORRECT credential -> rotates (same ref, new ciphertext)
    r = await cliente.put(
        f"/conexoes/{cid}", json={"config": CONFIG, "credencial": SENHA_BOA}, headers=h
    )
    assert r.status_code == 204, r.text
    assert "wrong-password" not in r.text and SENHA_BOA not in r.text  # no secret in the response

    ref_depois = await _cred_ref(owner_engine, cid)
    assert ref_depois == ref_antes  # ref preserved
    # now testing is ok (resolves the new secret)
    t = await cliente.post(f"/conexoes/{cid}/testar", headers=h)
    assert t.status_code == 200 and t.json()["ok"] is True


async def test_editar_sem_segredo_no_diff_de_auditoria(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="cxed3@example.com", perfil="especialista"
    )
    cid = await _criar(cliente, h, "edit-aud", segredo="wrong-password")
    await cliente.put(
        f"/conexoes/{cid}", json={"config": CONFIG, "credencial": SENHA_BOA}, headers=h
    )

    async with owner_engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT antes, depois FROM auditoria WHERE recurso_tipo='conexao' AND recurso_id = :r "
                        "AND acao='editar' ORDER BY id DESC LIMIT 1"
                    ),
                    {"r": cid},
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    diff = f"{row['antes']}{row['depois']}"
    assert SENHA_BOA not in diff and "wrong-password" not in diff  # the secret is never in the diff
    assert row["depois"]["credencial"] == "rotacionada"  # marker without the value


async def test_editar_rejeita_segredo_na_config(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="cxed4@example.com", perfil="especialista"
    )
    cid = await _criar(cliente, h, "edit-ruim")
    r = await cliente.put(
        f"/conexoes/{cid}", json={"config": {"host": "x", "senha": "vazou"}}, headers=h
    )
    assert r.status_code == 400 and r.json()["erro"]["codigo"] == "segredo_em_config"


async def test_editar_e_renomear_negados_para_nao_dono(owner_engine, app_engine, cliente):
    _, dono = await usuario_logado(
        owner_engine, cliente, email="cxeddono@example.com", perfil="especialista"
    )
    cid = await _criar(cliente, dono, "owners-cx")
    _, outro = await usuario_logado(
        owner_engine, cliente, email="cxedoutro@example.com", perfil="especialista"
    )
    assert (
        await cliente.put(f"/conexoes/{cid}", json={"config": CONFIG}, headers=outro)
    ).status_code == 403
    assert (
        await cliente.patch(f"/conexoes/{cid}", json={"nome": "x"}, headers=outro)
    ).status_code == 403
    # admin bypasses
    _, adm = await usuario_logado(
        owner_engine, cliente, email="cxedadm@example.com", perfil="admin"
    )
    assert (
        await cliente.put(f"/conexoes/{cid}", json={"config": CONFIG}, headers=adm)
    ).status_code == 204


async def test_renomear_conexao_feliz_e_conflito(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="cxren@example.com", perfil="especialista"
    )
    cid = await _criar(cliente, h, "cx-old")
    r = await cliente.patch(f"/conexoes/{cid}", json={"nome": "cx-new"}, headers=h)
    assert r.status_code == 200 and r.json()["nome"] == "cx-new"
    nomes = [c["nome"] for c in (await cliente.get("/conexoes", headers=h)).json()]
    assert "cx-new" in nomes and "cx-old" not in nomes

    # another connection; renaming to a name already in use -> 409 (UNIQUE column)
    cid2 = await _criar(cliente, h, "cx-taken")
    conflito = await cliente.patch(f"/conexoes/{cid2}", json={"nome": "cx-new"}, headers=h)
    assert conflito.status_code == 409 and conflito.json()["erro"]["codigo"] == "nome_em_uso"
