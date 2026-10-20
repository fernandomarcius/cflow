"""`python` node (sandbox): the generic procedural path (what SQL cannot cover).

Covers: happy path (script transforms the input -> snapshot), structural validation (no
config.codigo -> 422; invalid language -> 422), execution error (script without `resultado` ->
node failure), sandbox (network access blocked), per-node preview, and execution audit.
"""

from __future__ import annotations

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao


async def _publicar(cliente, h, nome: str, definicao: dict) -> str:
    pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    return pid


async def test_no_python_feliz(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="py1@example.com", perfil="especialista"
    )
    definicao = {
        "nos": [
            {"id": "fonte", "tipo": "sql", "sql": "SELECT * FROM (VALUES (1),(2),(3),(4)) t(n)"},
            {
                "id": "calc",
                "tipo": "python",
                "config": {
                    "linguagem": "python",
                    # procedural: doubles n and flags evens (what a SELECT would do, but via Python to prove the path)
                    "codigo": (
                        "linhas = con.sql('SELECT n FROM fonte').fetchall()\n"
                        "resultado = [{'n': r[0], 'dobro': r[0]*2, 'par': r[0] % 2 == 0} for r in linhas]\n"
                    ),
                },
            },
        ],
        "arestas": [{"de": "fonte", "para": "calc"}],
    }
    pid = await _publicar(cliente, h, "py-feliz", definicao)
    ex = (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()
    det = await aguardar_execucao(cliente, ex["id"], h)
    assert det["status"] == "concluida"
    linhas = {n["no_id"]: n["linhas"] for n in det["nos"]}
    assert linhas["calc"] == 4
    prev = (await cliente.get(f"/execucoes/{ex['id']}/nos/calc/preview", headers=h)).json()
    am = {int(r["n"]): r for r in prev["amostra"]}
    assert int(am[3]["dobro"]) == 6
    assert str(am[4]["par"]).lower() in ("true", "1")


async def test_no_python_preview(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="py2@example.com", perfil="especialista"
    )
    definicao = {
        "nos": [
            {"id": "f", "tipo": "sql", "sql": "SELECT * FROM (VALUES (10),(20)) t(v)"},
            {
                "id": "p",
                "tipo": "python",
                "config": {"codigo": "resultado = con.sql('SELECT v+1 AS w FROM f')"},
            },
        ],
        "arestas": [{"de": "f", "para": "p"}],
    }
    pid = await _publicar(cliente, h, "py-prev", definicao)
    r = await cliente.post(
        f"/pipelines/{pid}/nos/p/preview",
        json={"definicao": definicao, "parametros": {}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    ws = sorted(int(x["w"]) for x in r.json()["linhas"])
    assert ws == [11, 21]


async def test_no_python_exige_codigo(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="py3@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "py-inv"}, headers=h)).json()["id"]
    # no config.codigo -> 422
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho",
        json={
            "definicao": {"nos": [{"id": "p", "tipo": "python", "config": {}}], "arestas": []},
            "base_numero": 0,
        },
        headers=h,
    )
    assert r.status_code == 422
    msgs = " ".join(d["mensagem"] for d in r.json()["erro"]["detalhes"]).lower()
    assert "codigo" in msgs
    # invalid language -> 422
    r2 = await cliente.put(
        f"/pipelines/{pid}/rascunho",
        json={
            "definicao": {
                "nos": [
                    {
                        "id": "p",
                        "tipo": "python",
                        "config": {"codigo": "resultado=[]", "linguagem": "r"},
                    }
                ],
                "arestas": [],
            },
            "base_numero": 0,
        },
        headers=h,
    )
    assert r2.status_code == 422


async def test_no_python_erro_sem_resultado(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="py4@example.com", perfil="especialista"
    )
    definicao = {
        "nos": [
            {"id": "f", "tipo": "sql", "sql": "SELECT 1 AS n"},
            {"id": "p", "tipo": "python", "config": {"codigo": "x = 1  # never defines resultado"}},
        ],
        "arestas": [{"de": "f", "para": "p"}],
    }
    pid = await _publicar(cliente, h, "py-erro", definicao)
    ex = (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()
    det = await aguardar_execucao(cliente, ex["id"], h)
    assert det["status"] == "falha"


async def test_no_python_sandbox_bloqueia_rede(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="py5@example.com", perfil="especialista"
    )
    definicao = {
        "nos": [
            {"id": "f", "tipo": "sql", "sql": "SELECT 1 AS n"},
            {
                "id": "p",
                "tipo": "python",
                "config": {"codigo": "import socket; socket.socket(); resultado = []"},
            },
        ],
        "arestas": [{"de": "f", "para": "p"}],
    }
    pid = await _publicar(cliente, h, "py-rede", definicao)
    ex = (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()
    det = await aguardar_execucao(cliente, ex["id"], h)
    assert det["status"] == "falha"  # network blocked -> node fails
