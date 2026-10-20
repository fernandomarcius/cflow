"""Contract: the run index (`_indice.json`, and the current `<dataset>/_atual.json`) carries
`colunas` - the names of the published node columns, in order - ALWAYS, even with `particoes: []`.

With `permitir_vazio` and COLUMN partitioning the run produces no scope at all: the index states
the path format (`chaves`) and nothing else. Whoever serves the per-run publication still has to
answer "which columns does this dataset have" - and with no parquet to open, the index is the only
place to take that from. The manifest of each scope already carried `colunas`; the index did not.

No database: the advisory lock and the engine `SELECT pipeline_id` are replaced by doubles; the lake
is the local backend under `tmp`.
"""

from __future__ import annotations

import contextlib
import json
import uuid

import duckdb
import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.execucao import engine
from cflow_core.ingestao import leitura

EID = uuid.UUID("11111111-1111-4111-8111-111111111111")
PID = uuid.UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture
def lake(tmp_path, monkeypatch):
    from cflow_core.config import get_settings

    monkeypatch.setenv("CFLOW_LAKE_DIR", str(tmp_path / "lake"))
    monkeypatch.setenv("CFLOW_SNAPSHOT_DIR", str(tmp_path / "snap"))
    get_settings.cache_clear()

    @contextlib.asynccontextmanager
    async def _sem_trava(_chave):
        yield

    class _Resultado:
        def scalar_one_or_none(self):
            return PID

    class _Conn:
        async def execute(self, *_a, **_k):
            return _Resultado()

    @contextlib.asynccontextmanager
    async def _transacao():
        yield _Conn()

    monkeypatch.setattr(leitura, "trava_consultiva", _sem_trava)
    monkeypatch.setattr(engine, "transacao", _transacao)
    (tmp_path / "snap" / str(EID)).mkdir(parents=True)
    yield tmp_path / "lake"
    get_settings.cache_clear()


def _no(**cfg) -> dict:
    return {
        "id": "out",
        "tipo": "saida",
        "config": {"destino": "lake", "camada": "gold", "dataset": "ds_c2", **cfg},
    }


def _ler(lake, rel: str) -> dict:
    return json.loads((lake / "gold" / rel).read_text())


def test_prefixo_imutavel_gold_exige_raiz_e_silver_permanece_igual():
    cfg = [{"origem": "parametro", "nome": "operacao_id"}]
    base = {"operacao_id": "2"}
    assert (
        engine.prefixo_imutavel("ref/01/resumo", cfg, base, camada="gold", ambiente="prd")
        == "prd/ref/01/resumo/operacao_id=2"
    )
    assert engine.prefixo_imutavel("silver/base", cfg, base) == ("silver/base/operacao_id=2")
    with pytest.raises(ErroAplicacao, match="environment"):
        engine.prefixo_imutavel("ref/01/resumo", cfg, base, camada="gold")

    contraido = leitura.prefixo_imutavel_no_ambiente(
        "ref/01/resumo",
        "ref/01/resumo/ambiente=dev/operacao_id=2",
        "dev",
    )
    assert contraido == "dev/ref/01/resumo/operacao_id=2"
    assert "ambiente=" not in contraido
    with pytest.raises(ErroAplicacao) as cruzado:
        leitura.prefixo_imutavel_no_ambiente(
            "ref/01/resumo",
            "ref/01/resumo/ambiente=prd/operacao_id=2",
            "dev",
        )
    assert cruzado.value.codigo == "ambiente_lake_divergente"
    for contaminado in (
        "ref/01/resumo/operacao_id=2/ambiente=dev",
        "dev/ref/01/resumo/operacao_id=2/ambiente=dev",
    ):
        with pytest.raises(ErroAplicacao) as fora_da_primeira:
            leitura.prefixo_imutavel_no_ambiente("ref/01/resumo", contaminado, "dev")
        assert fora_da_primeira.value.codigo == "prefixo_imutavel_invalido"


async def test_corrida_sem_escopo_publica_indice_com_colunas(lake):
    duck = duckdb.connect()
    duck.execute('CREATE TABLE "out" (project VARCHAR, valor DECIMAL(18, 2), n INTEGER)')
    no = _no(permitir_vazio=True, particao=[{"origem": "coluna", "nome": "project"}])

    await engine._publicar_saida(
        duck, no, "out", 0, execucao_id=EID, parametros={"ambiente": "dev"}
    )

    imutavel = _ler(lake, f"dev/ds_c2/execucao_id={EID}/_indice.json")
    assert imutavel["particoes"] == []
    assert imutavel["colunas"] == ["project", "valor", "n"]
    assert _ler(lake, "dev/ds_c2/_atual.json")["colunas"] == ["project", "valor", "n"]


async def test_corrida_com_escopos_publica_colunas_na_ordem_do_no(lake):
    duck = duckdb.connect()
    duck.execute("CREATE TABLE \"out\" AS SELECT * FROM (VALUES (2, 'X'), (3, 'Y')) t(n, project)")
    no = _no(
        particao=[
            {"origem": "parametro", "nome": "operacao_id"},
            {"origem": "coluna", "nome": "project"},
        ]
    )

    await engine._publicar_saida(
        duck,
        no,
        "out",
        2,
        execucao_id=EID,
        parametros={"ambiente": "dev", "operacao_id": "61"},
    )

    imutavel = _ler(lake, f"dev/ds_c2/operacao_id=61/execucao_id={EID}/_indice.json")
    assert len(imutavel["particoes"]) == 2
    # The node order, not the partition key order nor the alphabetical one.
    assert imutavel["colunas"] == ["n", "project"]


async def test_publicar_indice_guarda_colunas_como_recebeu(lake):
    from cflow_core.ingestao import armazenamento

    await leitura.publicar_indice(
        lake=await armazenamento.abrir(None),
        dataset="ds_c2",
        camada="gold",
        execucao_id=EID,
        pipeline_id=PID,
        no_id="out",
        parametros={"ambiente": "dev"},
        chaves=["project"],
        particoes=[],
        colunas=["zeta", "alfa"],
        prefixo_imutavel="ds_c2",
    )

    assert _ler(lake, f"dev/ds_c2/execucao_id={EID}/_indice.json")["colunas"] == ["zeta", "alfa"]
    assert _ler(lake, "dev/ds_c2/_atual.json")["colunas"] == ["zeta", "alfa"]


async def test_silver_com_parametro_ambiente_preserva_caminhos_byte_a_byte(lake):
    from cflow_core.ingestao import armazenamento

    storage = await armazenamento.abrir(None)
    publicado = await leitura.gravar_dataset(
        lake=storage,
        dataset="silver_demo",
        execucao_id=EID,
        pipeline_id=PID,
        no_id="out",
        parquet=b"parquet",
        linhas=1,
        colunas=["valor"],
        camada="silver",
        particao={"operacao_id": "2"},
        parametros={"ambiente": "dev", "operacao_id": "2"},
    )
    await leitura.publicar_indice(
        lake=storage,
        dataset="silver_demo",
        camada="silver",
        execucao_id=EID,
        pipeline_id=PID,
        no_id="out",
        parametros={"ambiente": "dev", "operacao_id": "2"},
        chaves=["operacao_id"],
        particoes=[publicado],
        colunas=["valor"],
        prefixo_imutavel="silver_demo/operacao_id=2",
    )

    base = lake / "silver" / "silver_demo" / "operacao_id=2"
    assert (base / f"execucao_id={EID}" / "dados.parquet").read_bytes() == b"parquet"
    assert (base / f"execucao_id={EID}" / "_indice.json").is_file()
    assert (lake / "silver" / "silver_demo" / "_atual.json").is_file()
    assert not (lake / "silver" / "dev").exists()
