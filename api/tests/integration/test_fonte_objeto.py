"""`origem: "objeto"` source through the ENGINE: a `data_locked` file, by the path built from the
parameters - the previous month of historical snapshots that a dataset looks for after gold.

What is pinned end to end (validation -> publishing -> run -> audit trail):
- file present: the node is the parquet, and the existence marker has one row;
- file MISSING with `opcional`: the run COMPLETES, with an empty table on the declared columns -
  which keeps a month without a snapshot from taking down the closing;
- lineage records WHICH object was read (the external writer rewrites the same path).
"""

from __future__ import annotations

import duckdb
import pytest
from sqlalchemy import text

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

CAMINHO = (
    "Archive_{competencia_m1:%Y}/Sales/unit_{operacao_id}/"
    "Sales_{competencia_m1:fim_mes:%Y_%m_%d}.parquet"
)


@pytest.fixture
def inline(monkeypatch, tmp_path):
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_LAKE_CACHE_DIR", str(tmp_path / "cache"))
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "usar_temporal", False)
    yield
    get_settings.cache_clear()


def _definicao(conexao_id: str) -> dict:
    fonte = {
        "conexao_id": conexao_id,
        "origem": "objeto",
        "camada": "data_locked",
        "caminho": CAMINHO,
        "opcional": True,
    }
    return {
        "parametros": [
            {"nome": "operacao_id", "tipo": "texto", "obrigatorio": True},
            {"nome": "competencia_m1", "tipo": "texto", "obrigatorio": True},
        ],
        "nos": [
            {"id": "sales", "tipo": "fonte", "config": {**fonte, "colunas": ["saleId"]}},
            {"id": "marca", "tipo": "fonte", "config": {**fonte, "modo": "existencia"}},
            {
                "id": "conta",
                "tipo": "sql",
                "sql": "SELECT (SELECT count(*) FROM sales) AS v, (SELECT count(*) FROM marca) AS m",
            },
        ],
        "arestas": [{"de": "sales", "para": "conta"}, {"de": "marca", "para": "conta"}],
    }


async def _pipeline(cliente, h, conexao_id: str) -> str:
    pid = (await cliente.post("/pipelines", json={"nome": "objeto-dl"}, headers=h)).json()["id"]
    r = await cliente.put(
        f"/pipelines/{pid}/rascunho",
        json={"definicao": _definicao(conexao_id), "base_numero": 0},
        headers=h,
    )
    assert r.status_code in (200, 201), r.text
    r = await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    assert r.status_code in (200, 201), r.text
    return pid


async def _executar(cliente, h, pid: str, competencia_m1: str) -> dict:
    r = await cliente.post(
        f"/pipelines/{pid}/executar",
        json={"parametros": {"operacao_id": "61", "competencia_m1": competencia_m1}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    return (await cliente.get(f"/execucoes/{r.json()['id']}", headers=h)).json()


async def test_le_o_arquivo_do_data_locked_e_tolera_o_ausente(
    owner_engine, cliente, inline, tmp_path
):
    _, h = await usuario_logado(
        owner_engine, cliente, email="objeto-dl@example.com", perfil="admin"
    )
    raiz = tmp_path / "dl"
    r = await cliente.post(
        "/conexoes",
        json={
            "nome": "Data Locked (test)",
            "tipo": "objeto",
            "config": {"provedor": "local", "raiz": str(raiz)},
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    pid = await _pipeline(cliente, h, r.json()["id"])

    arquivo = raiz / "data_locked/Archive_2026/Sales/unit_61/Sales_2026_07_31.parquet"
    arquivo.parent.mkdir(parents=True)
    with duckdb.connect() as con:
        con.execute(f"COPY (SELECT unnest(['a', 'b']) AS saleId) TO '{arquivo}'")

    presente = await _executar(cliente, h, pid, "2026-07-01")
    assert presente["status"] == "concluida", presente
    linhas = {n["no_id"]: n["linhas"] for n in presente["nos"]}
    assert linhas["sales"] == 2 and linhas["marca"] == 1

    # August has no external snapshot: it completes, with an empty table - nothing is taken down.
    ausente = await _executar(cliente, h, pid, "2026-08-01")
    assert ausente["status"] == "concluida", ausente
    linhas = {n["no_id"]: n["linhas"] for n in ausente["nos"]}
    assert linhas["sales"] == 0 and linhas["marca"] == 0

    async with owner_engine.connect() as conn:
        lidos = (
            (
                await conn.execute(
                    text(
                        "SELECT depois FROM auditoria WHERE recurso_tipo = 'leitura_lake' "
                        "AND recurso_id = :e"
                    ),
                    {"e": presente["id"]},
                )
            )
            .scalars()
            .all()
        )
    assert any(
        d.get("objeto", "").endswith("Sales_2026_07_31.parquet") and d["existe"] for d in lidos
    ), lidos
