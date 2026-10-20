"""Pure parts of the obsolete stamp (`cflow_core.ingestao.carimbo`).

Writing, the audit trail and RBAC are in `api/tests/integration/test_publicados_carimbo.py`. What
stays here is what decides BEFORE touching anything: how the scope is read and why a scope is
refused.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.ingestao import carimbo
from cflow_core.ingestao.carimbo import Produtor


def _p(nome: str, *chaves: str, conexao: str | None = None) -> Produtor:
    return Produtor(pipeline_id=f"id-{nome}", nome=nome, conexao_id=conexao, chaves=tuple(chaves))


def test_particao_preserva_a_ordem_do_caminho():
    # The hive path is positional: the key order IS the pointer address.
    p = carimbo.interpretar_particao(["operacao_id=11", "data_registro=2026-08-06 12:17:23"])
    assert list(p) == ["operacao_id", "data_registro"]
    # date normalised to the DAY by the same rule as publishing - otherwise the stamp aims at another path
    assert p["data_registro"] == "2026-08-06"


@pytest.mark.parametrize(
    "itens",
    [[], None, "operacao_id=11", ["operacao_id"], ["=11"], ["op id=1"], ["a=1", "a=2"]],
)
def test_particao_malformada_e_recusada(itens):
    with pytest.raises(ErroAplicacao) as e:
        carimbo.interpretar_particao(itens)
    assert e.value.codigo == "particao_invalida"


@pytest.mark.parametrize("valor", ["../x", "a/b", "a=b", ""])
def test_valor_que_nao_vira_caminho_e_recusado_nunca_consertado(valor):
    with pytest.raises(ErroAplicacao) as e:
        carimbo.interpretar_particao([f"operacao_id={valor}"])
    assert e.value.codigo == "particao_valor_invalido"


def test_chaves_da_saida_seguem_a_regra_do_engine():
    cfg = {
        "particao": [
            {"origem": "parametro", "nome": "Operacao_ID"},
            {"origem": "coluna", "nome": "PROJECTID", "chave": "project_id"},
        ]
    }
    assert carimbo._chaves_da_saida(cfg) == ("operacao_id", "project_id")
    assert carimbo._chaves_da_saida({}) == ()


def test_escopo_do_layout_ainda_publicado_e_recusado_antes_de_tudo():
    # Even with a missing pointer: the first question is "is this layout still published?".
    prods = [_p("carga", "operacao_id")]
    erro = carimbo._bloqueio_de_carimbar(prods, {"operacao_id": "11"}, None)
    assert erro is not None and erro.codigo == "escopo_do_layout_vigente"
    assert "carga" in erro.mensagem


def test_dois_layouts_publicados_ao_mesmo_tempo_nao_tem_vigente():
    prods = [_p("a", "operacao_id", "data_registro"), _p("b", "operacao_id", "lote")]
    erro = carimbo._bloqueio_de_carimbar(prods, {"project": "1"}, {"caminho": "x"})
    assert erro is not None and erro.codigo == "layout_vigente_ambiguo"


def test_ponteiro_ausente_e_indice_nao_sao_carimbados():
    prods = [_p("a", "operacao_id", "data_registro")]
    assert carimbo._bloqueio_de_carimbar(prods, {"operacao_id": "1"}, None).codigo == (
        "escopo_ausente"
    )
    indice = {"chaves": ["operacao_id"], "particoes": []}
    assert carimbo._bloqueio_de_carimbar(prods, {"operacao_id": "1"}, indice).codigo == (
        "escopo_nao_e_ponteiro"
    )
    assert carimbo._bloqueio_de_carimbar(prods, {"operacao_id": "1"}, {"caminho": "c"}) is None


def test_plano_diz_o_estado_e_o_que_se_pode_fazer():
    prods = [_p("a", "operacao_id", "data_registro")]
    corpo = {"caminho": "ds/operacao_id=1/execucao_id=e1/dados.parquet", "execucao_id": "e1"}
    plano = carimbo._plano("gold", "ds", {"operacao_id": "1"}, prods, corpo)
    assert plano["estado"] == "vigente" and plano["pode_carimbar"] is True
    assert plano["pode_descarimbar"] is False and plano["bloqueio"] is None
    assert plano["layout_vigente"] == ["operacao_id", "data_registro"]
    assert plano["caminho"] == "ds/operacao_id=1/_atual.json"
    assert plano["aponta"]["execucao_id"] == "e1"

    carimbado = {**corpo, "obsoleto": {"chaves": ["operacao_id", "data_registro"]}}
    plano = carimbo._plano("gold", "ds", {"operacao_id": "1"}, prods, carimbado)
    assert plano["estado"] == "obsoleto"
    assert plano["pode_carimbar"] is False and plano["pode_descarimbar"] is True


async def test_carimbo_gold_exige_ambiente_e_isola_path_e_lock(monkeypatch):
    with pytest.raises(ErroAplicacao) as ausente:
        await carimbo.consultar(camada="gold", dataset="ds", particao_bruta=["operacao_id=1"])
    assert ausente.value.codigo == "ambiente_lake_invalido"

    travas = []
    gravacoes = []

    @contextlib.asynccontextmanager
    async def trava(chave):
        travas.append(chave)
        yield object()

    async def produtores(_conn, _camada, _dataset):
        return [_p("atual", "operacao_id", "competencia")]

    async def ler_json(_lake, _camada, _caminho):
        return {"caminho": "legado", "execucao_id": "e1"}

    class Lake:
        async def gravar(self, camada, caminho, _dados, **_kwargs):
            gravacoes.append((camada, caminho))

    async def abrir(_conexao):
        return Lake()

    async def registrar(*_args, **_kwargs):
        return None

    monkeypatch.setattr(carimbo, "trava_consultiva", trava)
    monkeypatch.setattr(carimbo, "produtores", produtores)
    monkeypatch.setattr(carimbo, "_ler_json", ler_json)
    monkeypatch.setattr(carimbo.armazenamento, "abrir", abrir)
    monkeypatch.setattr(carimbo, "registrar", registrar)

    for ambiente in ("dev", "prd"):
        resultado = await carimbo.carimbar(
            camada="gold",
            dataset="ds",
            particao_bruta=["operacao_id=1"],
            ambiente=ambiente,
            motivo="previous layout",
            ator_id=uuid.uuid4(),
        )
        assert resultado["caminho"] == f"{ambiente}/ds/operacao_id=1/_atual.json"

    assert travas == ["cflow:lake:gold:dev/ds", "cflow:lake:gold:prd/ds"]
    assert gravacoes == [
        ("gold", "dev/ds/operacao_id=1/_atual.json"),
        ("gold", "prd/ds/operacao_id=1/_atual.json"),
    ]
    assert carimbo._caminho("ds", {"operacao_id": "1"}) == ("ds/operacao_id=1/_atual.json")


def test_conexao_ambigua_e_produtor_ausente():
    with pytest.raises(ErroAplicacao) as e:
        carimbo._conexao([], "gold", "ds")
    assert e.value.codigo == "publicado_sem_produtor" and e.value.status == 404
    with pytest.raises(ErroAplicacao) as e:
        carimbo._conexao([_p("a", "x", conexao="c1"), _p("b", "x", conexao="c2")], "gold", "ds")
    assert e.value.codigo == "publicado_conexao_ambigua"


def test_escopos_do_indice_seguem_a_ordem_das_chaves_do_layout():
    doc = {"chaves": ["op", "project"], "particoes": [{"particao": {"project": "1_80", "op": 11}}]}
    [escopo] = carimbo.escopos_do_indice(doc, ("op", "project"))
    assert list(escopo.items()) == [("op", "11"), ("project", "1_80")]
    # Valid index that published no scope at all (`permitir_vazio`): empty list, not None.
    assert carimbo.escopos_do_indice({"chaves": ["op"], "particoes": []}, ("op",)) == []


@pytest.mark.parametrize(
    "documento",
    [
        None,
        {},
        {"chaves": ["op"], "particoes": "x"},
        {"chaves": ["outra"], "particoes": []},
        {"chaves": ["op"], "particoes": [{"particao": {"outra": "1"}}]},
        {"chaves": ["op"], "particoes": [{"caminho": "sem particao"}]},
    ],
)
def test_documento_que_nao_e_o_indice_do_layout_devolve_none(documento):
    # None, and not []: "unknown" counted as "no scope" would hide the gap.
    assert carimbo.escopos_do_indice(documento, ("op",)) is None


def test_ordem_de_exibicao_poe_numero_como_numero():
    escopos = [{"op": "11"}, {"op": "b"}, {"op": "2"}]
    assert sorted(escopos, key=lambda p: carimbo._ordem(("op",), p)) == [
        {"op": "2"},
        {"op": "11"},
        {"op": "b"},
    ]


@pytest.mark.parametrize("motivo", [None, "", "   ", 12])
def test_motivo_e_obrigatorio(motivo):
    with pytest.raises(ErroAplicacao) as e:
        carimbo._motivo(motivo)
    assert e.value.codigo == "motivo_obrigatorio"
