"""Promotion between environments through the v2 bundle (`/admin/exportar` + `/admin/importar`).

Invariants checked:
  - exports the PUBLISHED version (never the draft), the CLOSURE of what was requested in
    dependency order, the classification (layer/group/system) and only the connections the closure
    uses, without secrets;
  - `simular` writes nothing and returns the plan the import would execute;
  - imports in dependency order even when the file is out of order, re-pointing `conexao_id` and
    the `pipeline_id` of the pipeline-type source by NAME on the target, and applies the
    classification;
  - importing again creates nothing; an existing, different one only gets a new version with
    `atualizar`;
  - a reference to a pipeline outside the bundle and a cycle are loud refusals, writing nothing;
  - what belongs to the environment (credential, ingestion, output path, library, pinned version)
    becomes a named pending item;
  - only admins export and import.

"Another environment" is simulated by renaming the bundle: new names match nothing and everything
is created.
"""

from __future__ import annotations

import copy
import tempfile
import uuid

import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration


async def _publicar(cliente, h, nome: str, definicao: dict) -> str:
    pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code < 300, r.text
    r = await cliente.post(
        f"/pipelines/{pid}/publicar",
        json={"base_numero": r.json()["numero"], "nota": "v1"},
        headers=h,
    )
    assert r.status_code < 300, r.text
    return pid


async def _cenario(cliente, h) -> dict:
    """A (gold, load, sistema) reads the connection `promo-cx`; B (silver) reads A through a `pipeline`
    source; C has nothing to do with the two; D was never published."""
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write("a\n1\n")
    cx = (
        await cliente.post(
            "/conexoes",
            json={"nome": "promo-cx", "tipo": "arquivo", "config": {"caminho": f.name}},
            headers=h,
        )
    ).json()["id"]
    await cliente.post(
        "/conexoes",
        json={"nome": "promo-outra", "tipo": "arquivo", "config": {"caminho": f.name}},
        headers=h,
    )
    a = await _publicar(
        cliente,
        h,
        "promo-A",
        {
            "nos": [{"id": "n1", "tipo": "fonte", "config": {"conexao_id": cx, "sql": "SELECT 1"}}],
            "arestas": [],
        },
    )
    for rota, corpo in (("camada", {"camada": "gold"}), ("grupo", {"grupo": "load"})):
        assert (
            await cliente.patch(f"/pipelines/{a}/{rota}", json=corpo, headers=h)
        ).status_code == 204
    r = await cliente.patch(f"/pipelines/{a}/sistema", json={"sistema": True}, headers=h)
    assert r.status_code == 200, r.text
    b = await _publicar(
        cliente,
        h,
        "promo-B",
        {
            "nos": [
                {
                    "id": "f",
                    "tipo": "fonte",
                    "config": {"origem": "pipeline", "pipeline_id": a, "no_id": "n1"},
                },
                {"id": "s", "tipo": "sql", "sql": "SELECT * FROM f"},
            ],
            "arestas": [{"de": "f", "para": "s"}],
        },
    )
    assert (
        await cliente.patch(f"/pipelines/{b}/camada", json={"camada": "silver"}, headers=h)
    ).status_code == 204
    c = await _publicar(
        cliente,
        h,
        "promo-C",
        {"nos": [{"id": "q", "tipo": "sql", "sql": "SELECT 2"}], "arestas": []},
    )
    d = (await cliente.post("/pipelines", json={"nome": "promo-D"}, headers=h)).json()["id"]
    return {"cx": cx, "a": a, "b": b, "c": c, "d": d}


def _renomear(pacote: dict, sufixo: str) -> dict:
    p = copy.deepcopy(pacote)
    for item in p["pipelines"] + p["conexoes"]:
        item["nome"] += sufixo
    return p


async def _contagens(owner_engine) -> tuple:
    async with owner_engine.connect() as conn:
        contagens = []
        for t in ("pipelines", "pipeline_versoes", "conexoes", "auditoria"):
            contagens.append((await conn.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one())
        return tuple(contagens)


async def _admin(owner_engine, cliente, email: str):
    return (await usuario_logado(owner_engine, cliente, email=email, perfil="admin"))[1]


async def _exportar(cliente, h, *ids) -> dict:
    q = "&".join(f"pipeline={i}" for i in ids)
    r = await cliente.get(f"/admin/exportar{'?' + q if q else ''}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def test_exporta_o_fecho_publicado_com_a_classificacao(owner_engine, cliente):
    h = await _admin(owner_engine, cliente, "promo1@example.com")
    ids = await _cenario(cliente, h)
    # A draft after publication: the PUBLISHED one is what travels.
    det = (await cliente.get(f"/pipelines/{ids['a']}", headers=h)).json()
    rascunho = copy.deepcopy(det["definicao"])
    rascunho["nos"][0]["config"]["sql"] = "SELECT 'draft'"
    r = await cliente.put(
        f"/pipelines/{ids['a']}/rascunho",
        json={"definicao": rascunho, "base_numero": det["ultimo"]},
        headers=h,
    )
    assert r.status_code < 300, r.text

    pacote = await _exportar(cliente, h, ids["b"])
    assert pacote["versao"] == 2
    assert [p["nome"] for p in pacote["pipelines"]] == ["promo-A", "promo-B"]  # dependency first
    a = pacote["pipelines"][0]
    assert a["definicao"]["nos"][0]["config"]["sql"] == "SELECT 1"
    assert (a["camada"], a["grupo"], a["sistema"]) == ("gold", "load", True)
    assert [c["nome"] for c in pacote["conexoes"]] == ["promo-cx"]  # only the one the closure uses
    assert pacote["conexoes"][0]["tem_credencial"] is False

    tudo = await _exportar(cliente, h)
    nomes = {p["nome"] for p in tudo["pipelines"]}
    assert {"promo-A", "promo-B", "promo-C"} <= nomes and "promo-D" not in nomes
    assert "promo-D" in tudo["sem_versao_publicada"]


async def test_simular_nao_grava_e_importar_cria_em_ordem_reapontando(owner_engine, cliente):
    h = await _admin(owner_engine, cliente, "promo2@example.com")
    ids = await _cenario(cliente, h)
    pacote = _renomear(await _exportar(cliente, h, ids["b"]), "-2")
    pacote["pipelines"].reverse()  # file out of order: B before A

    antes = await _contagens(owner_engine)
    r = await cliente.post("/admin/importar", json={"bundle": pacote, "simular": True}, headers=h)
    assert r.status_code == 200, r.text
    plano = r.json()
    assert plano["simulado"] is True and await _contagens(owner_engine) == antes
    assert [(p["nome"], p["acao"]) for p in plano["pipelines"]] == [
        ("promo-A-2", "criar"),
        ("promo-B-2", "criar"),
    ]
    assert plano["conexoes"] == [{"nome": "promo-cx-2", "tipo": "arquivo", "acao": "criar"}]

    r = await cliente.post("/admin/importar", json={"bundle": pacote}, headers=h)
    assert r.status_code == 200, r.text
    res = r.json()
    assert (res["conexoes_criadas"], res["pipelines_criados"]) == (1, 2)

    lista = {p["nome"]: p for p in (await cliente.get("/pipelines", headers=h)).json()}
    a2, b2 = lista["promo-A-2"], lista["promo-B-2"]
    assert (a2["camada"], a2["grupo"], a2["sistema"]) == ("gold", "load", True)
    assert b2["camada"] == "silver"
    cx2 = next(
        c["id"]
        for c in (await cliente.get("/conexoes", headers=h)).json()
        if c["nome"] == "promo-cx-2"
    )
    def_a2 = (await cliente.get(f"/pipelines/{a2['id']}", headers=h)).json()["definicao"]
    def_b2 = (await cliente.get(f"/pipelines/{b2['id']}", headers=h)).json()["definicao"]
    assert def_a2["nos"][0]["config"]["conexao_id"] == cx2
    assert def_b2["nos"][0]["config"]["pipeline_id"] == a2["id"]  # re-pointed to the TARGET

    async with owner_engine.connect() as conn:
        acoes = {
            r[0]
            for r in (
                await conn.execute(
                    text("SELECT acao FROM auditoria WHERE recurso_id = CAST(:p AS uuid)"),
                    {"p": b2["id"]},
                )
            ).all()
        }
    assert {"criar", "publicar"} <= acoes

    # Again: everything equal, nothing created.
    res = (await cliente.post("/admin/importar", json={"bundle": pacote}, headers=h)).json()
    assert (res["conexoes_criadas"], res["pipelines_criados"], res["pipelines_atualizados"]) == (
        0,
        0,
        0,
    )
    assert {p["acao"] for p in res["pipelines"]} == {"manter"}


async def test_existente_diferente_so_atualiza_quando_pedido(owner_engine, cliente):
    h = await _admin(owner_engine, cliente, "promo3@example.com")
    ids = await _cenario(cliente, h)
    pacote = _renomear(await _exportar(cliente, h, ids["b"]), "-3")
    assert (
        await cliente.post("/admin/importar", json={"bundle": pacote}, headers=h)
    ).status_code == 200

    pacote["pipelines"][1]["definicao"]["nos"][1]["sql"] = "SELECT * FROM f WHERE 1 = 1"
    res = (await cliente.post("/admin/importar", json={"bundle": pacote}, headers=h)).json()
    b = next(p for p in res["pipelines"] if p["nome"] == "promo-B-3")
    assert b["acao"] == "manter_diferente" and "definicao" in b["diferencas"]
    assert res["pipelines_atualizados"] == 0

    res = (
        await cliente.post("/admin/importar", json={"bundle": pacote, "atualizar": True}, headers=h)
    ).json()
    assert res["pipelines_atualizados"] == 1
    b_imp = next(
        p for p in (await cliente.get("/pipelines", headers=h)).json() if p["nome"] == "promo-B-3"
    )
    det = (await cliente.get(f"/pipelines/{b_imp['id']}", headers=h)).json()
    assert det["definicao"]["nos"][1]["sql"] == "SELECT * FROM f WHERE 1 = 1"


async def test_referencia_fora_do_pacote_e_ciclo_sao_recusa_sem_gravar(owner_engine, cliente):
    h = await _admin(owner_engine, cliente, "promo4@example.com")
    ids = await _cenario(cliente, h)
    pacote = _renomear(await _exportar(cliente, h, ids["b"]), "-4")
    so_b = {**pacote, "pipelines": [p for p in pacote["pipelines"] if p["nome"] == "promo-B-4"]}
    antes = await _contagens(owner_engine)
    r = await cliente.post("/admin/importar", json={"bundle": so_b}, headers=h)
    assert r.status_code == 422 and r.json()["erro"]["codigo"] == "pacote_referencia_quebrada"
    assert ids["a"] in r.text

    x, y = str(uuid.uuid4()), str(uuid.uuid4())

    def le(de: str) -> dict:
        return {
            "nos": [
                {
                    "id": "f",
                    "tipo": "fonte",
                    "config": {"origem": "pipeline", "pipeline_id": de, "no_id": "f"},
                }
            ],
            "arestas": [],
        }

    ciclo = {
        "versao": 2,
        "conexoes": [],
        "pipelines": [
            {"id": x, "nome": "ciclo-X", "definicao": le(y)},
            {"id": y, "nome": "ciclo-Y", "definicao": le(x)},
        ],
    }
    r = await cliente.post("/admin/importar", json={"bundle": ciclo}, headers=h)
    assert r.status_code == 422 and r.json()["erro"]["codigo"] == "pacote_com_ciclo"
    assert await _contagens(owner_engine) == antes


async def test_o_que_e_do_ambiente_vira_pendencia(owner_engine, cliente):
    h = await _admin(owner_engine, cliente, "promo5@example.com")
    u, m = str(uuid.uuid4()), str(uuid.uuid4())
    pacote = {
        "versao": 2,
        "conexoes": [
            {
                "id": "c1",
                "nome": "pend-cx",
                "tipo": "postgres",
                "config": {},
                "tem_credencial": True,
            }
        ],
        "pipelines": [
            {
                "id": u,
                "nome": "pend-U",
                "definicao": {
                    "nos": [{"id": "a", "tipo": "sql", "sql": "SELECT 1"}],
                    "arestas": [],
                },
            },
            {
                "id": m,
                "nome": "pend-M",
                "definicao": {
                    "bibliotecas": ["bib-origem"],
                    "nos": [
                        {"id": "i", "tipo": "ingestao", "config": {"fonte_id": "fonte-origem"}},
                        {"id": "o", "tipo": "saida", "config": {"caminho": "/mnt/origem/x"}},
                        {
                            "id": "p",
                            "tipo": "fonte",
                            "config": {
                                "origem": "pipeline",
                                "pipeline_id": u,
                                "no_id": "a",
                                "versao": 7,
                            },
                        },
                    ],
                    "arestas": [],
                },
            },
        ],
    }
    r = await cliente.post("/admin/importar", json={"bundle": pacote, "simular": True}, headers=h)
    assert r.status_code == 200, r.text
    pend = " | ".join(r.json()["pendencias"])
    for trecho in ("pend-cx", "fonte-origem", "/mnt/origem/x", "version 7", "bib-origem"):
        assert trecho in pend, (trecho, pend)


async def test_so_admin_promove(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="promo6@example.com", perfil="especialista"
    )
    assert (await cliente.get("/admin/exportar", headers=h)).status_code == 403
    r = await cliente.post(
        "/admin/importar",
        json={"bundle": {"versao": 2, "conexoes": [], "pipelines": []}},
        headers=h,
    )
    assert r.status_code == 403
