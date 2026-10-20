"""CALENDAR-derived parameter, end to end through the API.

The execution computes the derived value, STORES it and BINDS it in the SQL; a supplied value that
differs from the computed one is rejected before any execution exists (an equal one passes, which
is what an external scheduler sends while it still does the computation itself); and in a chain the
upstream derives its own value from what the root received, without the root declaring anything.
"""

from __future__ import annotations

from sqlalchemy import text

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao

DERIVADO = {"origem": "competencia", "de": "competencia", "meses": -1}
PARAMETROS = [
    {"nome": "competencia", "tipo": "competencia", "obrigatorio": True},
    {"nome": "competencia_m1", "tipo": "texto", "derivacao": DERIVADO},
]
LE_M1 = {"id": "q", "tipo": "sql", "sql": "SELECT CAST(:competencia_m1 AS VARCHAR) AS m1"}


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


async def _parametros_guardados(owner_engine, execucao_id: str) -> dict:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT parametros FROM execucoes WHERE id = :e"), {"e": execucao_id}
            )
        ).scalar_one()


async def test_a_execucao_calcula_guarda_e_vincula_o_derivado(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="der1@example.com", perfil="especialista"
    )
    pid = await _publicar(
        cliente, h, "Derived - solo", {"parametros": PARAMETROS, "nos": [LE_M1], "arestas": []}
    )
    ex = await cliente.post(
        f"/pipelines/{pid}/executar", json={"parametros": {"competencia": "2026-08"}}, headers=h
    )
    assert ex.status_code < 300, ex.text
    det = await aguardar_execucao(cliente, ex.json()["id"], h)
    assert det["status"] == "concluida", det

    guardados = await _parametros_guardados(owner_engine, ex.json()["id"])
    assert guardados["competencia_m1"] == "2026-07-01"
    prev = (await cliente.get(f"/execucoes/{ex.json()['id']}/nos/q/preview", headers=h)).json()
    assert prev["amostra"][0]["m1"] == "2026-07-01"  # reached the SQL, not just the table


async def test_informado_diferente_e_recusado_antes_de_existir_execucao(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="der2@example.com", perfil="especialista"
    )
    pid = await _publicar(
        cliente,
        h,
        "Derived - checked",
        {"parametros": PARAMETROS, "nos": [LE_M1], "arestas": []},
    )
    r = await cliente.post(
        f"/pipelines/{pid}/executar",
        json={"parametros": {"competencia": "2026-08", "competencia_m1": "2026-06-01"}},
        headers=h,
    )
    assert r.status_code == 400, r.text
    assert r.json()["erro"]["codigo"] == "parametro_derivado_divergente"
    async with owner_engine.connect() as conn:
        n = (
            await conn.execute(
                text("SELECT count(*) FROM execucoes WHERE pipeline_id = :p"), {"p": pid}
            )
        ).scalar_one()
    assert n == 0

    # A value equal to the computed one passes (external schedulers may still compute it themselves).
    r = await cliente.post(
        f"/pipelines/{pid}/executar",
        json={"parametros": {"competencia": "2026-08", "competencia_m1": "2026-07-01"}},
        headers=h,
    )
    assert r.status_code < 300, r.text
    assert (await aguardar_execucao(cliente, r.json()["id"], h))["status"] == "concluida"


async def test_na_cadeia_a_montante_deriva_do_que_a_raiz_recebeu(owner_engine, cliente):
    """The root declares only `competencia`: the chain passes each pipeline what it declares, and
    the upstream computes ITS OWN `competencia_m1`."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="der3@example.com", perfil="especialista"
    )
    montante = await _publicar(
        cliente,
        h,
        "Derived - upstream",
        {"parametros": PARAMETROS, "nos": [{**LE_M1, "id": "hist"}], "arestas": []},
    )
    raiz = await _publicar(
        cliente,
        h,
        "Derived - root",
        {
            "parametros": [PARAMETROS[0]],
            "nos": [
                {
                    "id": "f",
                    "tipo": "fonte",
                    "config": {"origem": "pipeline", "pipeline_id": montante, "no_id": "hist"},
                },
                {"id": "c", "tipo": "sql", "sql": "SELECT m1 FROM f"},
            ],
            "arestas": [{"de": "f", "para": "c"}],
        },
    )
    ex = await cliente.post(
        f"/pipelines/{raiz}/executar", json={"parametros": {"competencia": "2026-01"}}, headers=h
    )
    assert ex.status_code < 300, ex.text
    det = await aguardar_execucao(cliente, ex.json()["id"], h)
    assert det["status"] == "concluida", det
    prev = (await cliente.get(f"/execucoes/{ex.json()['id']}/nos/c/preview", headers=h)).json()
    assert prev["amostra"][0]["m1"] == "2025-12-01"  # year rollover, computed in the upstream
