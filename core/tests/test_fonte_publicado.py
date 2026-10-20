"""`origem: "publicado"` source - read the ARTIFACT, do not rerun whoever produced it.

A reference pipeline used to read the dataset through `origem: "pipeline"`, which made the DAG
**rerun the load** on every run: the dataset was recomputed once per consumer. The right way is for
the load to publish the parquet and for the reference to read the parquet.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.ingestao import leitura
from cflow_core.pipelines.definicao import validar


def _publicar_em(
    raiz: Path,
    dataset: str,
    particao: dict[str, str],
    execucao: str,
    linhas: int,
    *,
    ambiente: str = "dev",
) -> str:
    """Mimic the `saida` node on a COMPOSITE-partition dataset: immutable parquet, scope pointer and
    the run index at `<dataset>/_atual.json` - with `chaves`, which is what `_na_ordem_do_dataset`
    reads to build the prefix in the dataset order, and not the jsonb one."""
    prefixo = leitura.prefixo_escopo(dataset, particao, ambiente=ambiente)
    base = f"{prefixo}/execucao_id={execucao}"
    (raiz / "gold" / base).mkdir(parents=True, exist_ok=True)
    caminho = raiz / "gold" / base / "dados.parquet"
    pq.write_table(pa.table({"v": pa.array(list(range(linhas)))}), caminho)
    (raiz / "gold" / prefixo / "_atual.json").write_text(
        json.dumps(
            {
                "dataset": dataset,
                "camada": "gold",
                "execucao_id": execucao,
                "parametros": {"ambiente": ambiente},
                "momento": "2026-09-08T01:00:00-03:00",
                "linhas": linhas,
                "particao": dict(particao),
                "caminho": f"{base}/dados.parquet",
            }
        )
    )
    (raiz / "gold" / ambiente / dataset / "_atual.json").write_text(
        json.dumps(
            {
                "dataset": dataset,
                "camada": "gold",
                "execucao_id": execucao,
                "parametros": {"ambiente": ambiente},
                "chaves": list(particao),
                "particoes": [{"particao": dict(particao), "caminho": f"{base}/dados.parquet"}],
            }
        )
    )
    return str(caminho)


def _publicar(raiz: Path, dataset: str, escopo: str | None, execucao: str, linhas: int) -> str:
    """Mimic the `saida` node: parquet at an IMMUTABLE path + scope pointer."""
    # The path stored in the pointer is relative TO THE LAYER (it goes as an argument to
    # `materializar`), so it does NOT include "gold/" - only the disk has the layer in the middle.
    prefixo = f"dev/{dataset}" + (f"/operacao_id={escopo}" if escopo else "")
    base = f"{prefixo}/execucao_id={execucao}"
    (raiz / "gold" / base).mkdir(parents=True, exist_ok=True)
    caminho = raiz / "gold" / base / "dados.parquet"
    pq.write_table(pa.table({"v": pa.array(list(range(linhas)))}), caminho)
    (raiz / "gold" / prefixo / "_atual.json").write_text(
        json.dumps(
            {
                "dataset": dataset,
                "camada": "gold",
                "execucao_id": execucao,
                "parametros": {"ambiente": "dev"},
                "momento": "2026-09-06T01:00:00-03:00",
                "linhas": linhas,
                # RELATIVE to the layer, as real publishing writes it. An absolute path here hid
                # that `read_parquet` received a path DuckDB cannot reach.
                "caminho": f"{base}/dados.parquet",
            }
        )
    )
    return str(caminho)


@pytest.fixture
def lake(tmp_path, monkeypatch):
    monkeypatch.setenv("CFLOW_LAKE_DIR", str(tmp_path))
    from cflow_core import config

    config.get_settings.cache_clear()
    yield tmp_path
    config.get_settings.cache_clear()


async def test_le_a_publicacao_corrente_do_escopo(lake):
    _publicar(lake, "orders", "26", "aaa", 10)
    cfg = {"dataset": "orders", "camada": "gold", "particao": {"operacao_id": ":operacao_id"}}
    ponteiro, caminhos = await leitura.resolver_publicado(
        cfg, parametros={"ambiente": "dev", "operacao_id": "26"}
    )
    assert ponteiro["execucao_id"] == "aaa"
    # The returned path must be a file that EXISTS - it is what goes to `read_parquet`.
    assert len(caminhos) == 1 and Path(caminhos[0]).is_file()


async def test_ponteiro_dev_nao_pode_materializar_manifesto_de_prd(lake):
    _publicar(lake, "orders", "26", "dev-run", 1)
    ponteiro = lake / "gold/dev/orders/operacao_id=26/_atual.json"
    corpo = json.loads(ponteiro.read_text())
    corpo["particao"] = {"operacao_id": "26"}
    corpo["caminho"] = "prd/orders/operacao_id=26/execucao_id=prd-run/dados.parquet"
    ponteiro.write_text(json.dumps(corpo))

    with pytest.raises(ErroAplicacao) as erro:
        await leitura.resolver_publicado(
            {
                "dataset": "orders",
                "camada": "gold",
                "particao": {"operacao_id": ":operacao_id"},
            },
            parametros={"ambiente": "dev", "operacao_id": "26"},
        )
    assert erro.value.codigo == "lake_ponteiro_invalido"


async def test_indice_dev_nao_pode_referenciar_particao_de_prd(lake):
    raiz = lake / "gold/dev/orders"
    raiz.mkdir(parents=True)
    (raiz / "_atual.json").write_text(
        json.dumps(
            {
                "dataset": "orders",
                "camada": "gold",
                "parametros": {"ambiente": "dev"},
                "chaves": ["operacao_id"],
                "particoes": [
                    {
                        "particao": {"operacao_id": "26"},
                        "caminho": ("prd/orders/operacao_id=26/execucao_id=prd-run/dados.parquet"),
                    }
                ],
            }
        )
    )

    with pytest.raises(ErroAplicacao) as erro:
        await leitura.resolver_publicado(
            {"dataset": "orders", "camada": "gold"},
            parametros={"ambiente": "dev"},
        )
    assert erro.value.codigo == "lake_ponteiro_invalido"


def test_documento_gold_exige_parametro_do_mesmo_ambiente_e_silver_nao_cria_raiz():
    gold = {
        "dataset": "ref/01/summary",
        "camada": "gold",
        "execucao_id": "e1",
        "parametros": {"ambiente": "dev"},
        "particao": {},
        "caminho": "dev/ref/01/summary/execucao_id=e1/dados.parquet",
    }
    leitura.validar_documento_publicado(
        gold, dataset="ref/01/summary", camada="gold", ambiente="dev"
    )
    com_parametro_cruzado = {
        **gold,
        "parametros": {"ambiente": "prd"},
    }
    with pytest.raises(ErroAplicacao) as cruzado:
        leitura.validar_documento_publicado(
            com_parametro_cruzado,
            dataset="ref/01/summary",
            camada="gold",
            ambiente="dev",
        )
    assert cruzado.value.codigo == "lake_ponteiro_invalido"

    silver = {
        "dataset": "base",
        "camada": "silver",
        "execucao_id": "e1",
        "parametros": {"ambiente": "prd"},
        "particao": {},
        "caminho": "base/execucao_id=e1/dados.parquet",
    }
    leitura.validar_documento_publicado(silver, dataset="base", camada="silver", ambiente="dev")
    assert not silver["caminho"].startswith(("dev/", "prd/"))
    with pytest.raises(ErroAplicacao) as invalido:
        leitura.validar_documento_publicado(
            {**silver, "parametros": {"ambiente": "qa"}},
            dataset="base",
            camada="silver",
            ambiente=None,
        )
    assert invalido.value.codigo == "lake_ponteiro_invalido"


async def test_gold_prd_le_particao_fisica_com_ambiente_nos_parametros(lake):
    _publicar_em(
        lake,
        "ref/01/summary",
        {"operacao_id": "2", "competencia": "2026-08-01"},
        "prd-ago",
        1,
        ambiente="prd",
    )
    cfg = {
        "dataset": "ref/01/summary",
        "camada": "gold",
        "particao": {
            "operacao_id": ":operacao_id",
            "competencia": ":competencia",
        },
    }

    ponteiro, caminhos = await leitura.resolver_publicado(
        cfg,
        parametros={
            "ambiente": "prd",
            "operacao_id": "2",
            "competencia": "2026-08-01",
        },
    )

    assert ponteiro["particao"] == {"operacao_id": "2", "competencia": "2026-08-01"}
    assert "/gold/prd/ref/01/summary/" in caminhos[0]


def test_prefixo_silver_permanece_byte_a_byte_sem_raiz_de_ambiente():
    assert leitura.prefixo_escopo("silver/base", {"operacao_id": "2"}) == (
        "silver/base/operacao_id=2"
    )


async def test_varias_cargas_no_mesmo_dia_convergem_para_a_ULTIMA(lake):
    """The pointer is moved on every publication. Without that - reading by glob - the two loads
    would add up, and the total would come out doubled, plausible and wrong."""
    _publicar(lake, "orders", "26", "primeira", 10)
    _publicar(lake, "orders", "26", "segunda", 7)
    cfg = {"dataset": "orders", "camada": "gold", "particao": {"operacao_id": ":operacao_id"}}
    ponteiro, caminhos = await leitura.resolver_publicado(
        cfg, parametros={"ambiente": "dev", "operacao_id": "26"}
    )
    assert ponteiro["execucao_id"] == "segunda"
    assert caminhos == [
        str(lake / "gold/dev/orders/operacao_id=26/execucao_id=segunda/dados.parquet")
    ]
    # And the previous load STAYS on disk: the path is immutable, only the pointer moves.
    assert (lake / "gold/dev/orders/operacao_id=26/execucao_id=primeira/dados.parquet").is_file()


async def test_cada_operacao_le_a_SUA_publicacao(lake):
    """Without a per-unit partition, `<dataset>/_atual.json` describes the last unit to finish,
    whoever it was. That is why loads publish partitioned."""
    _publicar(lake, "orders", "26", "da26", 10)
    _publicar(lake, "orders", "61", "da61", 999)
    cfg = {"dataset": "orders", "camada": "gold", "particao": {"operacao_id": ":operacao_id"}}
    p26, _ = await leitura.resolver_publicado(
        cfg, parametros={"ambiente": "dev", "operacao_id": "26"}
    )
    p61, _ = await leitura.resolver_publicado(
        cfg, parametros={"ambiente": "dev", "operacao_id": "61"}
    )
    assert (p26["execucao_id"], p61["execucao_id"]) == ("da26", "da61")


async def test_dataset_nunca_publicado_e_erro_nomeado(lake):
    cfg = {"dataset": "missing_dataset", "camada": "gold"}
    with pytest.raises(ErroAplicacao) as e:
        await leitura.resolver_publicado(cfg, parametros={"ambiente": "dev"})
    assert e.value.codigo == "lake_dataset_ausente"
    # The message used to say "silver" fixed, for any layer. An error message is an operations tool:
    # sending the investigator to the wrong bucket costs the first hour of diagnosis.
    assert "gold" in e.value.mensagem and "silver" not in e.value.mensagem


def test_a_definicao_recusa_pipeline_id_na_origem_publicado():
    """`pipeline_id` here would rerun the producer - exactly what this origin exists to stop doing."""
    d = {
        "nos": [
            {
                "id": "src",
                "tipo": "fonte",
                "config": {"origem": "publicado", "dataset": "orders", "pipeline_id": "x"},
            },
            {"id": "s", "tipo": "sql", "sql": "SELECT * FROM src"},
        ],
        "arestas": [{"de": "src", "para": "s"}],
    }
    with pytest.raises(ErroAplicacao) as e:
        validar(d)
    assert any("pipeline_id" in x.mensagem for x in e.value.detalhes)


def test_a_definicao_recusa_camada_que_um_no_de_saida_nao_publica():
    d = {
        "nos": [
            {
                "id": "src",
                "tipo": "fonte",
                "config": {"origem": "publicado", "dataset": "x", "camada": "bronze"},
            },
            {"id": "s", "tipo": "sql", "sql": "SELECT * FROM src"},
        ],
        "arestas": [{"de": "src", "para": "s"}],
    }
    with pytest.raises(ErroAplicacao):
        validar(d)


async def test_particao_de_DUAS_chaves_le_na_ordem_do_dataset__nao_na_do_jsonb(lake) -> None:
    """The hive path is POSITIONAL; the source `particao` is a JSON object - and the pipeline
    definition lives in a `jsonb` column, which does not preserve key order.

    `{operacao_id, competencia}` is written and Postgres returns `{competencia, operacao_id}`
    (sorted by length, then bytes). Building the prefix in dictionary order, the read points at
    `competencia=.../operacao_id=...`, which does not exist - a 404 on a published partition.

    It never showed up while every published dataset had ONE key, and with one key there is no
    order to lose.
    """
    tmp_path = lake
    dataset, exec_id = "ref05_summary_forecast", "11111111-1111-1111-1111-111111111111"
    prefixo = f"dev/{dataset}/operacao_id=188/competencia=2026-07-01"
    base = f"{prefixo}/execucao_id={exec_id}"
    (tmp_path / "gold" / base).mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"tpc": pa.array([10.0, 20.0])}), tmp_path / "gold" / base / "dados.parquet"
    )
    (tmp_path / "gold" / prefixo / "_atual.json").write_text(
        json.dumps(
            {
                "dataset": dataset,
                "camada": "gold",
                "execucao_id": exec_id,
                "parametros": {"ambiente": "dev"},
                "caminho": f"{base}/dados.parquet",
            }
        )
    )
    # The dataset INDEX, which is what declares the order - written by `publicar_indice` from the
    # ORDERED list of the `saida` node.
    (tmp_path / "gold" / "dev" / dataset / "_atual.json").write_text(
        json.dumps(
            {
                "dataset": dataset,
                "camada": "gold",
                "parametros": {"ambiente": "dev"},
                "chaves": ["operacao_id", "competencia"],
                "particoes": [{"caminho": f"{base}/dados.parquet"}],
            }
        )
    )

    # The order jsonb returns: alphabetical, the REVERSE of the declared one.
    cfg = {
        "origem": "publicado",
        "dataset": dataset,
        "camada": "gold",
        "particao": {"competencia": ":competencia_m1", "operacao_id": ":operacao_id"},
    }
    _ponteiro, caminhos = await leitura.resolver_publicado(
        cfg,
        parametros={
            "ambiente": "dev",
            "operacao_id": "188",
            "competencia_m1": "2026-07-01",
        },
    )
    assert len(caminhos) == 1
    assert "operacao_id=188/competencia=2026-07-01" in caminhos[0]


async def test_particao_de_UMA_chave_nao_consulta_o_indice(lake) -> None:
    """With one key there is no order to lose, and the dataset may not even have an index (the case
    of loads that publish per scope without a run index). Reading the index as a prerequisite would
    break what works today."""
    _publicar(lake, "orders", "26", "22222222-2222-2222-2222-222222222222", 3)
    cfg = {
        "origem": "publicado",
        "dataset": "orders",
        "camada": "gold",
        "particao": {"operacao_id": ":operacao_id"},
    }
    _p, caminhos = await leitura.resolver_publicado(
        cfg, parametros={"ambiente": "dev", "operacao_id": "26"}
    )
    assert len(caminhos) == 1 and "operacao_id=26" in caminhos[0]


# --------------------------------------------------------------------------------------------
# As-of in the partition (the early run is intra-month and repeated)
# --------------------------------------------------------------------------------------------


async def test_duas_fotos_da_mesma_operacao_coexistem_por_as_of(lake):
    """The defect: a unit ran at three as-of values and the pointer ended up describing only the last
    one - a reference asking for July received September, with no error. With `data_registro` in
    the partition each snapshot has its own pointer, and both stay reachable.
    """
    _publicar_em(
        lake, "orders_ledger", {"operacao_id": "11", "data_registro": "2026-07-08"}, "j", 3
    )
    _publicar_em(
        lake, "orders_ledger", {"operacao_id": "11", "data_registro": "2026-09-03"}, "s", 9
    )
    cfg = {
        "dataset": "orders_ledger",
        "camada": "gold",
        "particao": {"operacao_id": ":operacao_id", "data_registro": ":data_registro"},
    }
    julho, _ = await leitura.resolver_publicado(
        cfg,
        parametros={
            "ambiente": "dev",
            "operacao_id": "11",
            "data_registro": "2026-07-08 23:59:59",
        },
    )
    setembro, _ = await leitura.resolver_publicado(
        cfg,
        parametros={
            "ambiente": "dev",
            "operacao_id": "11",
            "data_registro": "2026-09-03 23:59:59",
        },
    )
    # The `as_of` arrives with the end of day appended by the period resolver and is reduced to the
    # DAY when the path is built: without that the Jul 8 snapshot would come out in two scopes and
    # each reader would find half of the data.
    assert (julho["execucao_id"], setembro["execucao_id"]) == ("j", "s")
    assert julho["linhas"] == 3 and setembro["linhas"] == 9


async def test_ponteiro_carimbado_de_obsoleto_e_recusado(lake):
    """Previous partition layout: the object stays readable and STOPPED being updated. Serving it
    would give the migration-day snapshot forever - the defect partitioning exists to kill,
    re-entering through the old pointer door."""
    _publicar(lake, "orders", "11", "old", 5)
    ponteiro = lake / "gold/dev/orders/operacao_id=11/_atual.json"
    corpo = json.loads(ponteiro.read_text())
    ponteiro.write_text(
        json.dumps(
            {
                **corpo,
                leitura.MARCA_OBSOLETO: {
                    "chaves": ["operacao_id", "data_registro"],
                    "desde": "2026-09-08T10:00:00-03:00",
                },
            }
        )
    )
    cfg = {"dataset": "orders", "camada": "gold", "particao": {"operacao_id": ":operacao_id"}}
    with pytest.raises(ErroAplicacao) as e:
        await leitura.resolver_publicado(cfg, parametros={"ambiente": "dev", "operacao_id": "11"})
    assert e.value.codigo == "lake_escopo_obsoleto"
    # The message must say the dataset AND the current layout: without them, whoever gets the error
    # does not know which source to fix or to what.
    assert "orders" in e.value.mensagem
    assert "operacao_id/data_registro" in e.value.mensagem


async def test_o_carimbo_nao_alcanca_o_escopo_do_layout_novo(lake):
    """Stamping the old pointer must not hurt whoever already migrated - both live in the SAME
    dataset directory, which is exactly why `operacao_id` comes first."""
    _publicar(lake, "orders", "11", "old", 5)
    velho = lake / "gold/dev/orders/operacao_id=11/_atual.json"
    velho.write_text(
        json.dumps({**json.loads(velho.read_text()), leitura.MARCA_OBSOLETO: {"desde": "x"}})
    )
    _publicar_em(lake, "orders", {"operacao_id": "11", "data_registro": "2026-08-06"}, "nova", 7)

    cfg = {
        "dataset": "orders",
        "camada": "gold",
        "particao": {"operacao_id": ":operacao_id", "data_registro": ":data_registro"},
    }
    ponteiro, caminhos = await leitura.resolver_publicado(
        cfg,
        parametros={
            "ambiente": "dev",
            "operacao_id": "11",
            "data_registro": "2026-08-06",
        },
    )
    assert ponteiro["execucao_id"] == "nova" and ponteiro["linhas"] == 7
    assert Path(caminhos[0]).is_file()
