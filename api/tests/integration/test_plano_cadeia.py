"""`GET /pipelines/{id}/cadeia`: what a dispatch runs TOGETHER, without dispatching anything.

The scheduling screen queries this route before the click: scheduling a consumer runs its upstreams
with all their effects. The publish decision is the SAME as at dispatch (`decidir_montante`); cases
with external effects live in the pure test (`core/tests/test_plano_cadeia_puro.py`), because
publishing an upstream with e-mail would need a real SMTP server.
"""

from __future__ import annotations

from ._arnes_auth import usuario_logado

UPSTREAM = {
    "nos": [
        {"id": "src", "tipo": "sql", "sql": "SELECT 1 AS a"},
        {
            "id": "saida",
            "tipo": "saida",
            "config": {"destino": "lake", "camada": "gold", "dataset": "ds_cadeia"},
        },
    ],
    "arestas": [{"de": "src", "para": "saida"}],
}


def _consumidor(upstream_id: str, **topo) -> dict:
    return {
        "nos": [
            {
                "id": "f",
                "tipo": "fonte",
                "config": {"origem": "pipeline", "pipeline_id": upstream_id, "no_id": "src"},
            }
        ],
        "arestas": [],
        **topo,
    }


async def _criar(cliente, h, nome: str, definicao: dict, *, publicar: bool = True) -> str:
    pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code == 200, r.text
    if publicar:
        r = await cliente.post(
            f"/pipelines/{pid}/publicar",
            json={"base_numero": r.json()["numero"], "nota": "v1"},
            headers=h,
        )
        assert r.status_code == 201, r.text
    return pid


async def test_pipeline_solo_nao_tem_montante(cliente, owner_engine):
    _uid, h = await usuario_logado(
        owner_engine, cliente, email="cad-a@example.com", perfil="especialista"
    )
    pid = await _criar(cliente, h, "cadeia-solo", UPSTREAM)
    r = await cliente.get(f"/pipelines/{pid}/cadeia", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["montantes"] == [] and r.json()["recusa"] is None


async def test_consumidor_mostra_a_montante_e_o_que_ela_publica(cliente, owner_engine):
    _uid, h = await usuario_logado(
        owner_engine, cliente, email="cad-b@example.com", perfil="especialista"
    )
    up = await _criar(cliente, h, "cadeia-up", UPSTREAM)
    pid = await _criar(cliente, h, "cadeia-consumidor", _consumidor(up))
    r = await cliente.get(f"/pipelines/{pid}/cadeia", headers=h)
    assert r.status_code == 200, r.text
    plano = r.json()
    assert plano["nome"] == "cadeia-consumidor" and plano["sem_publicar"] is False
    [m] = plano["montantes"]
    assert m["pipeline_id"] == up and m["nome"] == "cadeia-up" and m["efeitos"] == []
    assert m["publicacoes"] == [
        {
            "no_id": "saida",
            "destino": "lake",
            "alvo": "ds_cadeia",
            "camada": "gold",
            "publica": True,
        }
    ]


async def test_montante_publica_false_na_raiz_desliga_a_publicacao(cliente, owner_engine):
    _uid, h = await usuario_logado(
        owner_engine, cliente, email="cad-c@example.com", perfil="especialista"
    )
    up = await _criar(cliente, h, "cadeia-up2", UPSTREAM)
    pid = await _criar(cliente, h, "cadeia-raiz", _consumidor(up, montante_publica=False))
    plano = (await cliente.get(f"/pipelines/{pid}/cadeia", headers=h)).json()
    assert plano["sem_publicar"] is True and plano["recusa"] is None
    assert plano["montantes"][0]["publicacoes"][0]["publica"] is False


async def test_upstream_sem_versao_publicada_e_erro_nomeado(cliente, owner_engine):
    # A schedule created on this chain would fail on EVERY dispatch: the UI must know beforehand.
    _uid, h = await usuario_logado(
        owner_engine, cliente, email="cad-d@example.com", perfil="especialista"
    )
    up = await _criar(cliente, h, "cadeia-rascunho", UPSTREAM, publicar=False)
    pid = await _criar(cliente, h, "cadeia-orfa", _consumidor(up), publicar=False)
    r = await cliente.get(f"/pipelines/{pid}/cadeia", headers=h)
    assert r.status_code >= 400, r.text
    assert r.json()["erro"]["codigo"] == "sem_definicao"


async def test_rbac_e_visibilidade(cliente, owner_engine):
    _aid, ha = await usuario_logado(
        owner_engine, cliente, email="cad-adm@example.com", perfil="admin"
    )
    pid = await _criar(cliente, ha, "cadeia-sistema", UPSTREAM)
    _oid, ho = await usuario_logado(
        owner_engine, cliente, email="cad-op@example.com", perfil="operador"
    )
    assert (await cliente.get(f"/pipelines/{pid}/cadeia", headers=ho)).status_code == 403
    assert (await cliente.get(f"/pipelines/{pid}/cadeia")).status_code == 401
    # A `sistema` pipeline stays invisible to non-admins, same rule as GET on the pipeline.
    r = await cliente.patch(f"/pipelines/{pid}/sistema", json={"sistema": True}, headers=ha)
    assert r.status_code in (200, 204), r.text
    _eid, he = await usuario_logado(
        owner_engine, cliente, email="cad-esp@example.com", perfil="especialista"
    )
    assert (await cliente.get(f"/pipelines/{pid}/cadeia", headers=he)).status_code == 403
    assert (await cliente.get(f"/pipelines/{pid}/cadeia", headers=ha)).status_code == 200
