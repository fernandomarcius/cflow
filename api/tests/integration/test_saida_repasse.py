"""A pure pass-through output does not re-encode the data.

Without column partitioning, the `saida` node is the predecessor Parquet: the snapshot becomes a
LINK to the immutable file of whoever came before (no materialisation, no encoding), and the content
hash - computed over the view - is that of the Parquet it reads, the same number the CTAS of that
view gave. With column partitioning, publishing sorts by `rowid`, so there the table stays
materialised.
"""

from __future__ import annotations

import json
import os

import duckdb
from sqlalchemy import text

from cflow_core.execucao import engine

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao, contrato_ambiente_gold, parametros_ambiente_gold


async def _rodar(cliente, h, nome: str, definicao: dict) -> dict:
    definicao = contrato_ambiente_gold(definicao)
    pid = (await cliente.post("/pipelines", json={"nome": nome}, headers=h)).json()["id"]
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    ex = (
        await cliente.post(
            f"/pipelines/{pid}/executar",
            json={"parametros": parametros_ambiente_gold(definicao)},
            headers=h,
        )
    ).json()
    return await aguardar_execucao(cliente, ex["id"], h)


async def _nos(owner_engine, execucao_id) -> dict:
    async with owner_engine.connect() as conn:
        linhas = (
            await conn.execute(
                text(
                    "SELECT no_id, snapshot, hash, linhas FROM execucao_nos "
                    "WHERE execucao_id = CAST(:e AS uuid)"
                ),
                {"e": str(execucao_id)},
            )
        ).all()
    return {r.no_id: r for r in linhas}


async def test_saida_de_repasse_e_o_parquet_do_predecessor(owner_engine, cliente, tmp_path):
    _, h = await usuario_logado(
        owner_engine, cliente, email="repasse1@example.com", perfil="especialista"
    )
    definicao = {
        "nos": [
            {
                "id": "src",
                "tipo": "sql",
                # a BIGINT sum becomes HUGEINT, which does not come back from Parquet with the same
                # type: this is the case where the output hash differs from the predecessor table one
                "sql": "SELECT n, c, sum(n) OVER () AS total "
                "FROM (VALUES (1,'a'),(2,'b'),(3,NULL)) t(n,c)",
            },
            {
                "id": "out",
                "tipo": "saida",
                "config": {"nome": "pass_through", "caminho": str(tmp_path / "export")},
            },
        ],
        "arestas": [{"de": "src", "para": "out"}],
    }
    det = await _rodar(cliente, h, "Pass-through", definicao)
    assert det["status"] == "concluida", det
    nos = await _nos(owner_engine, det["id"])
    src, out = nos["src"], nos["out"]
    assert os.path.samefile(out.snapshot, src.snapshot)  # link: zero encoding
    assert out.linhas == src.linhas
    # The output hash is that of what it READS - the predecessor Parquet -, not of its table: the
    # `sum` HUGEINT does not come back from Parquet as HUGEINT. It has always been so (before, via the
    # CTAS of the same view); what the view must not change is this number.
    duck = duckdb.connect()
    duck.execute(f"CREATE VIEW p AS SELECT * FROM read_parquet('{src.snapshot}')")
    assert out.hash == engine._hash_conteudo(duck, "p")
    assert os.path.isfile(tmp_path / "export" / "pass_through.parquet")  # and it published


async def test_saida_particionada_por_coluna_continua_materializando(
    owner_engine, cliente, tmp_path, monkeypatch
):
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    get_settings.cache_clear()
    try:
        _, h = await usuario_logado(
            owner_engine, cliente, email="repasse2@example.com", perfil="especialista"
        )
        definicao = {
            "nos": [
                {
                    "id": "src",
                    "tipo": "sql",
                    "sql": "SELECT * FROM (VALUES ('A',1),('A',2),('B',3)) t(project,n)",
                },
                {
                    "id": "out",
                    "tipo": "saida",
                    "config": {
                        "destino": "lake",
                        "camada": "gold",
                        "dataset": "pass_through_part",
                        "particao": [{"origem": "coluna", "nome": "project"}],
                    },
                },
            ],
            "arestas": [{"de": "src", "para": "out"}],
        }
        det = await _rodar(cliente, h, "Partitioned pass-through", definicao)
        assert det["status"] == "concluida", det
        nos = await _nos(owner_engine, det["id"])
        assert (nos["out"].hash, nos["out"].linhas) == (nos["src"].hash, nos["src"].linhas)
        indice = json.loads(
            (tmp_path / "lake" / "gold" / "dev" / "pass_through_part" / "_atual.json").read_text()
        )
        assert sorted(p["linhas"] for p in indice["particoes"]) == [1, 2]
    finally:
        get_settings.cache_clear()
