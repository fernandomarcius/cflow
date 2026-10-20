"""`origem: "objeto"` source: one file of the `data_locked` layer (a read-only archive written by
an external system), by path.

The case that motivated it: a dataset needs the previous month of historical snapshots, which the
external system wrote to the `data_locked` bucket
(`Archive_{year}/Sales/unit_{unit}/Sales_{day}.parquet`) and which gold never had.
"""

from __future__ import annotations

import duckdb
import pytest

from cflow_core.config import get_settings
from cflow_core.errors import ErroAplicacao
from cflow_core.ingestao import armazenamento, objeto
from cflow_core.ingestao.armazenamento import ArmazenamentoLocal
from cflow_core.pipelines.definicao import validar

MODELO = "Archive_{competencia_m1:%Y}/Sales/unit_{operacao_id}/Sales_{competencia_m1:fim_mes:%Y_%m_%d}.parquet"
PARAMS = {"operacao_id": "61", "competencia_m1": "2026-02-01"}


def _cfg(**extra) -> dict:
    return {
        "origem": "objeto",
        "conexao_id": "22222222-2222-2222-2222-222222222222",
        "camada": "data_locked",
        "caminho": MODELO,
        "opcional": True,
        "colunas": ["saleId"],
        **extra,
    }


# ------------------------------------------------------------------------------ path


def test_caminho_com_o_ultimo_dia_do_mes():
    assert (
        objeto.montar_caminho(MODELO, PARAMS)
        == "Archive_2026/Sales/unit_61/Sales_2026_02_28.parquet"
    )


def test_competencia_expandida_vale_pelo_inicio():
    """After normalisation, `competencia` only exists as `_inicio`/`_fim`."""
    assert (
        objeto.montar_caminho(
            "x_{competencia:%Y_%m}.parquet",
            {"competencia_inicio": "2026-08-01", "competencia_fim": "2026-09-01"},
        )
        == "x_2026_08.parquet"
    )


@pytest.mark.parametrize("valor", ["../gold", "1/2", "a b", ".."])
def test_valor_que_nao_e_segmento_seguro_e_recusado(valor):
    with pytest.raises(ErroAplicacao) as e:
        objeto.montar_caminho("unit_{operacao_id}/x.parquet", {"operacao_id": valor})
    assert e.value.codigo == "objeto_parametro_invalido"


def test_parametro_sem_valor_e_recusado():
    with pytest.raises(ErroAplicacao) as e:
        objeto.montar_caminho(MODELO, {"operacao_id": "61"})
    assert e.value.codigo == "objeto_parametro_ausente"


def test_formato_de_data_sobre_valor_que_nao_e_data():
    with pytest.raises(ErroAplicacao) as e:
        objeto.montar_caminho("{operacao_id:%Y}.parquet", {"operacao_id": "61"})
    assert e.value.codigo == "objeto_parametro_invalido"


# ------------------------------------------------------------------------------ validation


def test_config_valida():
    assert objeto.problemas_da_config(_cfg()) == []
    assert objeto.problemas_da_config(_cfg(modo="existencia", colunas=None)) == []


@pytest.mark.parametrize(
    ("extra", "trecho"),
    [
        ({"camada": "gold"}, "through the pointer"),
        ({"formato": "csv"}, "formato"),
        ({"modo": "tudo"}, "modo"),
        ({"colunas": None}, "requires config.colunas"),
        ({"caminho": "../x.parquet"}, "without '..'"),
        ({"caminho": "x_{a:%Q}.parquet"}, "format"),
        ({"dataset": "ds"}, "config.dataset"),
        ({"conexao_id": None}, "conexao_id"),
    ],
)
def test_config_invalida_e_recusada_nomeando(extra, trecho):
    problemas = objeto.problemas_da_config(_cfg(**extra))
    assert any(trecho in p for p in problemas), problemas


def test_origem_objeto_entra_na_validacao_da_definicao():
    definicao = {"nos": [{"id": "f", "tipo": "fonte", "config": _cfg(camada="silver")}]}
    with pytest.raises(ErroAplicacao) as e:
        validar(definicao)
    assert any("origem 'objeto'" in d.mensagem for d in e.value.detalhes)


# ------------------------------------------------------------------------------ reading


@pytest.fixture
def lake_local(tmp_path, monkeypatch):
    lake = ArmazenamentoLocal(raiz=tmp_path)

    async def _abrir(_conexao_id):
        return lake

    monkeypatch.setattr(objeto, "abrir", _abrir)
    return tmp_path


def _gravar_base(raiz, vendas: list[str]) -> None:
    destino = raiz / "data_locked" / "Archive_2026/Sales/unit_61/Sales_2026_02_28.parquet"
    destino.parent.mkdir(parents=True)
    with duckdb.connect() as con:
        con.execute("CREATE TABLE v (saleId VARCHAR)")
        for v in vendas:
            con.execute("INSERT INTO v VALUES (?)", [v])
        con.execute(f"COPY v TO '{destino}'")


async def test_ausente_e_opcional_vira_tabela_vazia_com_as_colunas(lake_local):
    lido = await objeto.resolver(_cfg(), parametros=PARAMS)
    assert lido["existe"] is False
    with duckdb.connect() as con:
        con.execute(f"CREATE TABLE t AS {objeto.sql_relacao(lido, _cfg())}")
        assert con.execute("SELECT * FROM t").fetchall() == []
        assert [d[0] for d in con.execute("SELECT * FROM t").description] == ["saleId"]
        marca = objeto.sql_relacao(lido, _cfg(modo="existencia"))
        assert con.execute(marca).fetchall() == []


async def test_ausente_sem_opcional_e_erro_nomeando_o_objeto(lake_local):
    with pytest.raises(ErroAplicacao) as e:
        await objeto.resolver(_cfg(opcional=False), parametros=PARAMS)
    assert e.value.codigo == "objeto_ausente"
    assert "Sales_2026_02_28.parquet" in e.value.mensagem


async def test_presente_le_o_arquivo_e_a_marca_existe_mesmo_vazio(lake_local):
    _gravar_base(lake_local, [])
    lido = await objeto.resolver(_cfg(), parametros=PARAMS)
    assert lido["existe"] is True
    with duckdb.connect() as con:
        assert con.execute(objeto.sql_relacao(lido, _cfg())).fetchall() == []
        # Written EMPTY is not "not written": the marker has the row.
        assert len(con.execute(objeto.sql_relacao(lido, _cfg(modo="existencia"))).fetchall()) == 1


class LakeRemoto:
    """A non-local backend: the bytes come through `ler`, and the file goes to the content-addressed cache."""

    provedor = "gcs"

    def __init__(self, objetos: dict[str, bytes]):
        self.objetos = objetos

    def uri(self, camada, caminho):
        return f"gs://{camada}/{caminho}"

    async def ler(self, camada, caminho):
        try:
            return self.objetos[caminho]
        except KeyError:
            raise ErroAplicacao(codigo="objeto_ausente", mensagem="x", status=404) from None


async def test_remoto_regravado_no_mesmo_caminho_nao_serve_a_versao_velha(tmp_path, monkeypatch):
    """The external writer REWRITES the file at the same path: a path-keyed cache would serve the old one."""
    monkeypatch.setenv("CFLOW_LAKE_CACHE_DIR", str(tmp_path / "cache"))
    get_settings.cache_clear()
    try:
        arquivos = []
        for vendas in (["a"], ["a", "b"]):
            origem = tmp_path / f"v{len(vendas)}.parquet"
            with duckdb.connect() as con:
                con.execute(f"COPY (SELECT unnest({vendas!r}) AS saleId) TO '{origem}'")
            caminho = objeto.montar_caminho(MODELO, PARAMS)
            lake = LakeRemoto({caminho: origem.read_bytes()})

            async def _abrir(_c, lake=lake):
                return lake

            monkeypatch.setattr(objeto, "abrir", _abrir)
            lido = await objeto.resolver(_cfg(), parametros=PARAMS)
            with duckdb.connect() as con:
                arquivos.append(con.execute(objeto.sql_relacao(lido, _cfg())).fetchall())
        assert arquivos == [[("a",)], [("a",), ("b",)]]
    finally:
        get_settings.cache_clear()


# ------------------------------------------------------------------------------ the layer


async def test_a_plataforma_nunca_grava_no_data_locked(tmp_path):
    lake = ArmazenamentoLocal(raiz=tmp_path)
    with pytest.raises(ErroAplicacao) as e:
        await lake.gravar("data_locked", "x.parquet", b"x")
    assert e.value.codigo == "camada_so_leitura"
    assert not (tmp_path / "data_locked").exists()


def test_conexao_so_com_o_data_locked_nao_exige_buckets_de_ingestao():
    assert armazenamento.validar_config({"provedor": "gcs", "buckets": {"data_locked": "dl"}}) == []
    # ...and the lake connection still requires landing/bronze/quarantine.
    assert armazenamento.validar_config({"provedor": "gcs", "buckets": {"gold": "g"}})
