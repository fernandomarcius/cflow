"""Run map (`GET /corridas/{cadeia_id}`): the executions of a chain and who depends on whom.

Edges are not stored at dispatch time; they are rebuilt from the definitions of the versions that
ran. The tests seed executions by INSERT (same setup as `test_execucoes_global`), with the really
published `versao_numero`, since that definition decides the edges.

What is proven: `pipeline` sources and `publicado` reads become edges between EXECUTIONS; a
`sistema` pipeline is hidden from non-admins and its node is bridged (otherwise the neighbours
would look independent); a missing run and an invisible run answer the same 404; operators are
denied.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

BASE = datetime.fromisoformat("2026-07-15T10:00:00-03:00")


async def _pipeline(cliente, h, nome: str, definicao: dict) -> str:
    pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    assert r.status_code in (200, 201), r.text
    r = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    assert r.status_code in (200, 201), r.text
    return pid


async def _semear(
    app_engine, pid: str, *, cadeia: uuid.UUID, raiz: bool = False, seg: int = 0
) -> str:
    """A completed execution of the pipeline's LATEST version, inside the chain."""
    async with app_engine.begin() as conn:
        numero = (
            await conn.execute(
                text("SELECT max(numero) FROM pipeline_versoes WHERE pipeline_id = :p"), {"p": pid}
            )
        ).scalar_one()
        momento = BASE + timedelta(seconds=seg)
        return str(
            (
                await conn.execute(
                    text(
                        "INSERT INTO execucoes (pipeline_id, versao_numero, status, iniciada_em, "
                        "  concluida_em, origem, cadeia_id, cadeia_raiz) "
                        "VALUES (:p, :n, CAST('concluida' AS status_execucao), :m, :m, 'manual', "
                        "  :c, :r) RETURNING id"
                    ),
                    {"p": pid, "n": numero, "m": momento, "c": cadeia, "r": raiz},
                )
            ).scalar_one()
        )


def _sql(consulta: str = "SELECT 1 AS n") -> dict:
    return {"nos": [{"id": "saida", "tipo": "sql", "sql": consulta}], "arestas": []}


def _consome(pid: str) -> dict:
    return {
        "nos": [
            {
                "id": "src",
                "tipo": "fonte",
                "config": {"origem": "pipeline", "pipeline_id": pid, "no_id": "saida"},
            },
            {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
        ],
        "arestas": [{"de": "src", "para": "cont"}],
    }


def _arestas(corpo: dict) -> set[tuple[str, str, str]]:
    return {(a["de"], a["para"], a["tipo"]) for a in corpo["arestas"]}


async def test_mapa_liga_as_execucoes_da_corrida(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="mapa1@example.com", perfil="especialista"
    )
    a = await _pipeline(cliente, h, "MapaA", _sql())
    b = await _pipeline(cliente, h, "MapaB", _consome(a))
    c = await _pipeline(cliente, h, "MapaC", _consome(b))
    cadeia = uuid.uuid4()
    ea = await _semear(app_engine, a, cadeia=cadeia)
    eb = await _semear(app_engine, b, cadeia=cadeia, seg=1)
    ec = await _semear(app_engine, c, cadeia=cadeia, raiz=True, seg=2)

    r = await cliente.get(f"/corridas/{cadeia}", headers=h)
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["cadeia_id"] == str(cadeia)
    assert corpo["raiz_id"] == ec
    assert {e["id"] for e in corpo["execucoes"]} == {ea, eb, ec}
    # Only the DIRECT dependency: C reads B, not A.
    assert _arestas(corpo) == {(ea, eb, "pipeline"), (eb, ec, "pipeline")}
    # Same item shape as `GET /execucoes`, so the UI reuses the run logic.
    item = next(e for e in corpo["execucoes"] if e["id"] == eb)
    assert item["pipeline_nome"] == "MapaB"
    assert "comecou_em" in item and item["cadeia_raiz"] is False


async def test_leitura_publicado_espera_o_produtor(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="mapa2@example.com", perfil="especialista"
    )
    produtor = await _pipeline(
        cliente,
        h,
        "MapaProducer",
        {
            "nos": [
                {"id": "dado", "tipo": "sql", "sql": "SELECT 1 AS n"},
                {
                    "id": "saida",
                    "tipo": "saida",
                    "config": {"destino": "lake", "camada": "gold", "dataset": "mapa_ds"},
                },
            ],
            "arestas": [{"de": "dado", "para": "saida"}],
        },
    )
    reader = await _pipeline(
        cliente,
        h,
        "MapaReader",
        {
            "nos": [
                {
                    "id": "src",
                    "tipo": "fonte",
                    "config": {"origem": "publicado", "dataset": "mapa_ds"},
                },
                {"id": "cont", "tipo": "sql", "sql": "SELECT count(*) AS c FROM src"},
            ],
            "arestas": [{"de": "src", "para": "cont"}],
        },
    )
    cadeia = uuid.uuid4()
    ep = await _semear(app_engine, produtor, cadeia=cadeia)
    el = await _semear(app_engine, reader, cadeia=cadeia, raiz=True, seg=1)

    corpo = (await cliente.get(f"/corridas/{cadeia}", headers=h)).json()
    assert _arestas(corpo) == {(ep, el, "publicado")}


async def test_pipeline_sistema_some_e_o_no_e_ponteado(owner_engine, app_engine, cliente):
    _, esp = await usuario_logado(
        owner_engine, cliente, email="mapa3e@example.com", perfil="especialista"
    )
    _, adm = await usuario_logado(owner_engine, cliente, email="mapa3a@example.com", perfil="admin")
    a = await _pipeline(cliente, adm, "MapaStart", _sql())
    hidden = await _pipeline(cliente, adm, "MapaHidden", _consome(a))
    c = await _pipeline(cliente, adm, "MapaEnd", _consome(hidden))
    r = await cliente.patch(f"/pipelines/{hidden}/sistema", json={"sistema": True}, headers=adm)
    assert r.status_code == 200, r.text
    cadeia = uuid.uuid4()
    ea = await _semear(app_engine, a, cadeia=cadeia)
    eo = await _semear(app_engine, hidden, cadeia=cadeia, seg=1)
    ec = await _semear(app_engine, c, cadeia=cadeia, raiz=True, seg=2)

    r = await cliente.get(f"/corridas/{cadeia}", headers=esp)
    assert r.status_code == 200, r.text
    assert "MapaHidden" not in r.text and eo not in r.text
    corpo = r.json()
    assert {e["id"] for e in corpo["execucoes"]} == {ea, ec}
    # Without the bridge, Inicio and Fim would look independent.
    assert _arestas(corpo) == {(ea, ec, "pipeline")}

    corpo_adm = (await cliente.get(f"/corridas/{cadeia}", headers=adm)).json()
    assert {e["id"] for e in corpo_adm["execucoes"]} == {ea, eo, ec}
    assert _arestas(corpo_adm) == {(ea, eo, "pipeline"), (eo, ec, "pipeline")}


async def test_inexistente_e_invisivel_respondem_o_mesmo_404(owner_engine, app_engine, cliente):
    _, esp = await usuario_logado(
        owner_engine, cliente, email="mapa4e@example.com", perfil="especialista"
    )
    _, adm = await usuario_logado(owner_engine, cliente, email="mapa4a@example.com", perfil="admin")
    interno = await _pipeline(cliente, adm, "MapaOnlyInternal", _sql())
    await cliente.patch(f"/pipelines/{interno}/sistema", json={"sistema": True}, headers=adm)
    cadeia = uuid.uuid4()
    await _semear(app_engine, interno, cadeia=cadeia, raiz=True)

    inexistente = await cliente.get(f"/corridas/{uuid.uuid4()}", headers=esp)
    invisivel = await cliente.get(f"/corridas/{cadeia}", headers=esp)
    assert inexistente.status_code == invisivel.status_code == 404
    e1, e2 = inexistente.json()["erro"], invisivel.json()["erro"]
    assert e1["codigo"] == e2["codigo"] == "corrida_inexistente"
    assert e1["mensagem"] == e2["mensagem"]

    assert (await cliente.get(f"/corridas/{cadeia}", headers=adm)).status_code == 200


async def test_operador_nao_ve_o_mapa(owner_engine, cliente):
    _, op = await usuario_logado(
        owner_engine, cliente, email="mapa5@example.com", perfil="operador"
    )
    r = await cliente.get(f"/corridas/{uuid.uuid4()}", headers=op)
    assert r.status_code == 403


async def test_sem_login_e_401(cliente):
    r = await cliente.get(f"/corridas/{uuid.uuid4()}")
    assert r.status_code == 401
