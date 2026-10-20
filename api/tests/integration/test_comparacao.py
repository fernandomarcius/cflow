"""Comparison node (diff by key): equivalence gate.

Covers: so_esquerda/so_direita/divergente classification (execution), omission of 'igual' and the
incluir_iguais option (preview), and structural validation (requires config.chave).
"""

from __future__ import annotations

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao

# e: {1:x, 2:y, 3:z}  ×  d: {2:y, 3:ZZ, 4:w}
_NOS = [
    {"id": "e", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1,'x'),(2,'y'),(3,'z')) t(n,c)"},
    {"id": "d", "tipo": "sql", "sql": "SELECT * FROM (VALUES (2,'y'),(3,'ZZ'),(4,'w')) t(n,c)"},
]
_ARESTAS = [{"de": "e", "para": "cmp"}, {"de": "d", "para": "cmp"}]


def _def(cmp_config: dict) -> dict:
    return {
        "nos": [*_NOS, {"id": "cmp", "tipo": "comparacao", "config": cmp_config}],
        "arestas": _ARESTAS,
    }


async def test_diff_por_chave_executando(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="cmp1@example.com", perfil="especialista"
    )
    definicao = _def({"chave": ["n"], "esquerda": "e", "direita": "d"})
    pid = (await cliente.post("/pipelines", json={"nome": "Diff"}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    ex = (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()
    det = await aguardar_execucao(cliente, ex["id"], h)
    assert det["status"] == "concluida"
    assert {n["no_id"]: n["linhas"] for n in det["nos"]}["cmp"] == 3  # 'igual' omitted

    prev = (await cliente.get(f"/execucoes/{ex['id']}/nos/cmp/preview", headers=h)).json()
    por_n = {int(r["n"]): r["_status"] for r in prev["amostra"]}
    assert por_n == {1: "so_esquerda", 4: "so_direita", 3: "divergente"}


async def test_incluir_iguais_no_preview(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="cmp2@example.com", perfil="especialista"
    )
    definicao = _def({"chave": ["n"], "esquerda": "e", "direita": "d", "incluir_iguais": True})
    pid = (await cliente.post("/pipelines", json={"nome": "Diff equals"}, headers=h)).json()["id"]
    r = await cliente.post(
        f"/pipelines/{pid}/nos/cmp/preview", json={"definicao": definicao}, headers=h
    )
    assert r.status_code == 200
    por_n = {int(x["n"]): x["_status"] for x in r.json()["linhas"]}
    assert por_n == {1: "so_esquerda", 2: "igual", 3: "divergente", 4: "so_direita"}


async def test_validacao_exige_chave(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="cmp3@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "Invalid diff"}, headers=h)).json()["id"]
    definicao = _def({"esquerda": "e", "direita": "d"})  # without 'chave'
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 422
    assert r.json()["erro"]["codigo"] == "grafo_invalido"
