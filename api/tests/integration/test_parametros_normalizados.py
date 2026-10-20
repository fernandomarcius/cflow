"""cFlow reports how it WOULD store an execution's parameters, without executing.

The one property that matters (it lets an external scheduler recognise a redelivery without
mirroring the normalisation itself) is that what this route returns is EXACTLY what the execution
stores. Proven by really executing, over a matrix of inputs exercising each normalisation rule:
text becoming an integer, a date becoming a cutoff with time, a competence becoming a window, the
default applied, blank = absent, a calendar-derived value and a redacted secret.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao

DERIVADO = {"origem": "competencia", "de": "competencia", "meses": -1}
DEF = {
    "parametros": [
        {"nome": "operacao_id", "tipo": "inteiro", "obrigatorio": True},
        {"nome": "competencia", "tipo": "competencia", "obrigatorio": True},
        {"nome": "data_registro", "tipo": "as_of", "obrigatorio": True},
        {"nome": "competencia_m1", "tipo": "texto", "derivacao": DERIVADO},
        {"nome": "carteira", "tipo": "texto", "default": "todas"},
        {"nome": "senha_api", "tipo": "texto", "segredo": True},
    ],
    "nos": [{"id": "q", "tipo": "sql", "sql": "SELECT 1 AS um"}],
    "arestas": [],
}
ENTRADAS = [
    {
        "operacao_id": "11",
        "competencia": "2026-08",
        "data_registro": "2026-08-06",
        "senha_api": "s3nh4",
    },
    {
        "operacao_id": 11,
        "competencia": "2026-01",
        "data_registro": "2026-01-31T18:05:09",
        "carteira": "",
        "senha_api": "x",
    },
    {
        "operacao_id": " 7 ",
        "competencia": "2025-12",
        "data_registro": "2025-12",
        "carteira": "retail",
        "competencia_m1": "2025-11-01",
        "senha_api": "y",
    },
]


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


async def _guardados(owner_engine, execucao_id: str) -> dict:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT parametros FROM execucoes WHERE id = :e"), {"e": execucao_id}
            )
        ).scalar_one()


async def _execucoes(owner_engine, pipeline_id: str) -> int:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT count(*) FROM execucoes WHERE pipeline_id = :p"), {"p": pipeline_id}
            )
        ).scalar_one()


async def test_o_que_a_rota_devolve_e_o_que_a_execucao_guarda(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="norm1@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h, "Normalised - matrix", DEF)
    previstos = []
    for entrada in ENTRADAS:
        r = await cliente.post(
            f"/pipelines/{pid}/parametros/normalizados", json={"parametros": entrada}, headers=h
        )
        assert r.status_code == 200, r.text
        corpo = r.json()
        assert corpo["fora_da_raiz"] == [] and corpo["relativos"] == []
        previstos.append(corpo["parametros"])
    # Asking executes nothing.
    assert await _execucoes(owner_engine, pid) == 0

    for entrada, previsto in zip(ENTRADAS, previstos, strict=True):
        ex = await cliente.post(
            f"/pipelines/{pid}/executar-encadeado", json={"parametros": entrada}, headers=h
        )
        assert ex.status_code < 300, ex.text
        assert (await aguardar_execucao(cliente, ex.json()["id"], h))["status"] == "concluida"
        assert await _guardados(owner_engine, ex.json()["id"]) == previsto

    # And each normalisation rule really shows in the response (not equality of empty things).
    assert previstos[0] == {
        "operacao_id": 11,
        "competencia_inicio": "2026-08-01",
        "competencia_fim": "2026-09-01",
        "data_registro": "2026-08-06 23:59:59",
        "competencia_m1": "2026-07-01",
        "carteira": "todas",
        "senha_api": "***",
    }
    assert previstos[1]["data_registro"] == "2026-01-31 18:05:09"
    assert previstos[1]["competencia_m1"] == "2025-12-01"
    assert previstos[1]["carteira"] == "todas"  # blank = absent = default
    assert previstos[2]["operacao_id"] == 7
    assert previstos[2]["data_registro"] == "2025-12-31 23:59:59"


async def test_nome_que_so_a_montante_declara_sai_em_fora_da_raiz(owner_engine, cliente):
    """In a chain the dispatch accepts the name and it vanishes from the root execution: the route
    says so BEFORE, and the real execution confirms the root does not store it."""
    _, h = await usuario_logado(
        owner_engine, cliente, email="norm2@example.com", perfil="especialista"
    )
    comp = {"nome": "competencia", "tipo": "competencia", "obrigatorio": True}
    montante = await _publicar(
        cliente,
        h,
        "Normalised - upstream",
        {
            "parametros": [comp, {"nome": "project", "tipo": "texto"}],
            "nos": [{"id": "s", "tipo": "sql", "sql": "SELECT 1 AS um"}],
            "arestas": [],
        },
    )
    raiz = await _publicar(
        cliente,
        h,
        "Normalised - root",
        {
            "parametros": [comp],
            "nos": [
                {
                    "id": "f",
                    "tipo": "fonte",
                    "config": {"origem": "pipeline", "pipeline_id": montante, "no_id": "s"},
                }
            ],
            "arestas": [],
        },
    )
    entrada = {"competencia": "2026-08", "project": "5"}
    r = await cliente.post(
        f"/pipelines/{raiz}/parametros/normalizados", json={"parametros": entrada}, headers=h
    )
    assert r.status_code == 200, r.text
    assert r.json()["fora_da_raiz"] == ["project"]
    assert "project" not in r.json()["parametros"]

    ex = await cliente.post(
        f"/pipelines/{raiz}/executar-encadeado", json={"parametros": entrada}, headers=h
    )
    assert ex.status_code < 300, ex.text
    assert (await aguardar_execucao(cliente, ex.json()["id"], h))["status"] == "concluida"
    assert await _guardados(owner_engine, ex.json()["id"]) == r.json()["parametros"]


async def test_valor_que_depende_do_dia_sai_em_relativos(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="norm3@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h, "Normalised - relative", DEF)
    r = await cliente.post(
        f"/pipelines/{pid}/parametros/normalizados",
        json={
            "parametros": {
                "operacao_id": "1",
                "competencia": "atual",
                "data_registro": "hoje",
                "senha_api": "z",
            }
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    # The derived value inherits: computed from a relative competence, it also changes tomorrow.
    assert set(r.json()["relativos"]) == {"competencia", "data_registro", "competencia_m1"}


@pytest.mark.parametrize(
    ("entrada", "codigo"),
    [
        (
            {"operacao_id": "1", "competencia": "2026-13", "data_registro": "2026-08-06"},
            "parametros_invalidos",
        ),
        (
            {
                "operacao_id": "1",
                "competencia": "2026-08",
                "data_registro": "2026-08-06",
                "competencia_m1": "2020-01-01",
            },
            "parametro_derivado_divergente",
        ),
    ],
)
async def test_entrada_invalida_e_a_mesma_recusa_do_disparo(owner_engine, cliente, entrada, codigo):
    _, h = await usuario_logado(
        owner_engine, cliente, email="norm4@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h, "Normalised - refusal", DEF)
    r = await cliente.post(
        f"/pipelines/{pid}/parametros/normalizados", json={"parametros": entrada}, headers=h
    )
    assert r.status_code == 400, r.text
    assert r.json()["erro"]["codigo"] == codigo
    ex = await cliente.post(
        f"/pipelines/{pid}/executar-encadeado", json={"parametros": entrada}, headers=h
    )
    assert ex.status_code == 400 and ex.json()["erro"]["codigo"] == codigo


async def test_so_quem_pode_executar_pergunta(owner_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="norm5@example.com", perfil="especialista"
    )
    pid = await _publicar(cliente, h, "Normalised - access", DEF)
    _, h_op = await usuario_logado(
        owner_engine, cliente, email="norm6@example.com", perfil="operador"
    )
    r = await cliente.post(
        f"/pipelines/{pid}/parametros/normalizados", json={"parametros": {}}, headers=h_op
    )
    assert r.status_code == 403
    r = await cliente.post(f"/pipelines/{pid}/parametros/normalizados", json={"parametros": {}})
    assert r.status_code == 401
