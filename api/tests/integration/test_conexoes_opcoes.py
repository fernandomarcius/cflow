"""Dynamic dropdown options endpoint (POST /conexoes/{id}/opcoes): value <-> label (name <-> id),
distinct, sorted by label. Backward compatible (column only = value=label) and open to operators."""

from __future__ import annotations

import tempfile

import pytest

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


async def _conexao_csv(cliente, h, nome, conteudo):
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        f.write(conteudo)
        caminho = f.name
    return (
        await cliente.post(
            "/conexoes",
            json={"nome": nome, "tipo": "arquivo", "config": {"caminho": caminho}},
            headers=h,
        )
    ).json()["id"]


async def test_opcoes_valor_e_rotulo(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="opc1@example.com", perfil="especialista"
    )
    # repeated id (15) -> distinct; sorted by label.
    cid = await _conexao_csv(cliente, h, "ops", "id,label\n15,GOLDEN\n9,ALFA\n15,GOLDEN\n")
    r = await cliente.post(
        f"/conexoes/{cid}/opcoes",
        json={"coluna_valor": "id", "coluna_rotulo": "label"},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json() == [{"valor": "9", "rotulo": "ALFA"}, {"valor": "15", "rotulo": "GOLDEN"}]


async def test_opcoes_legado_so_coluna(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="opc2@example.com", perfil="especialista"
    )
    cid = await _conexao_csv(cliente, h, "ops2", "uf\nSP\nRJ\nSP\n")
    # without coluna_rotulo -> label = value (backward compatible with {conexao_id, coluna}).
    r = await cliente.post(f"/conexoes/{cid}/opcoes", json={"coluna_valor": "uf"}, headers=h)
    assert r.status_code == 200
    assert r.json() == [{"valor": "RJ", "rotulo": "RJ"}, {"valor": "SP", "rotulo": "SP"}]


async def test_opcoes_acessivel_ao_operador(owner_engine, cliente):
    # The operator (not a specialist) needs to read options to fill in the filter, so the endpoint
    # only requires authentication.
    esp, h_esp = await usuario_logado(
        owner_engine, cliente, email="opc3@example.com", perfil="especialista"
    )
    cid = await _conexao_csv(cliente, h_esp, "ops3", "id,nome\n1,One\n2,Two\n")
    _, h_op = await usuario_logado(
        owner_engine, cliente, email="opc4@example.com", perfil="operador"
    )
    r = await cliente.post(
        f"/conexoes/{cid}/opcoes",
        json={"coluna_valor": "id", "coluna_rotulo": "nome"},
        headers=h_op,
    )
    assert r.status_code == 200
    assert {"valor": "1", "rotulo": "One"} in r.json()
