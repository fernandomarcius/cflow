"""`publicado` source with `opcional: true`: a scope that was NEVER published becomes an empty table.

It exists for the period that never closed. A cover pipeline reads a summary dataset for months m-1
and m-2 and its SQL already discards any competence not declared as locked - but the source used to
fail BEFORE that, with `lake_dataset_ausente`, and took down the chain. The earliest month exists
nowhere, so no seeding would bring it.
"""

from __future__ import annotations

import json

import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.ingestao import leitura


class LakeFalso:
    """Only what `_escopo_opcional_vazio` uses: `ler` of the dataset index."""

    def __init__(self, objetos: dict[str, bytes]):
        self.objetos = objetos
        self.lidos: list[str] = []

    async def ler(self, camada: str, caminho: str) -> bytes:
        self.lidos.append(f"{camada}/{caminho}")
        try:
            return self.objetos[caminho]
        except KeyError:
            raise ErroAplicacao(
                codigo="objeto_ausente", mensagem=f"there is no {caminho}", status=404
            ) from None


def _indice(colunas: list[str]) -> bytes:
    return json.dumps({"dataset": "ref05_summary_forecast", "colunas": colunas}).encode()


AUSENTE = ErroAplicacao(codigo="lake_dataset_ausente", mensagem="not published", status=400)


async def test_sem_a_marca_o_erro_continua_subindo():
    """The tolerance is OPT-IN: without it, a missing scope keeps failing - which is what tells
    'period that never closed' from 'the upstream did not publish what it should'."""
    lake = LakeFalso({"ref05_summary_forecast/_atual.json": _indice(["competencia", "tpc"])})
    assert (
        await leitura._escopo_opcional_vazio(
            {"dataset": "ref05_summary_forecast"},
            lake,
            "ref05_summary_forecast",
            "gold",
            {},
            AUSENTE,
        )
        is None
    )


async def test_com_a_marca_devolve_ponteiro_sintetico_com_as_colunas_do_indice():
    lake = LakeFalso(
        {"ref05_summary_forecast/_atual.json": _indice(["competencia", "ProjectId", "tpc"])}
    )
    vazio = await leitura._escopo_opcional_vazio(
        {"dataset": "ref05_summary_forecast", "opcional": True},
        lake,
        "ref05_summary_forecast",
        "gold",
        {"operacao_id": "188", "competencia": "2026-04-01"},
        AUSENTE,
    )
    assert vazio is not None
    assert vazio["colunas"] == ["competencia", "ProjectId", "tpc"]
    assert vazio["escopo_ausente"] is True
    assert vazio["particao"] == {"operacao_id": "188", "competencia": "2026-04-01"}
    # No invented provenance: there was no publication for this scope.
    assert vazio["execucao_id"] is None


async def test_dataset_que_nunca_foi_publicado_continua_erro():
    """Without an index there are no columns, and a table without a schema would trade a named error
    for 'column does not exist' three nodes later."""
    lake = LakeFalso({})
    assert (
        await leitura._escopo_opcional_vazio(
            {"dataset": "ref05_summary_forecast", "opcional": True},
            lake,
            "ref05_summary_forecast",
            "gold",
            {},
            AUSENTE,
        )
        is None
    )


async def test_indice_sem_colunas_continua_erro():
    lake = LakeFalso({"ref05_summary_forecast/_atual.json": _indice([])})
    assert (
        await leitura._escopo_opcional_vazio(
            {"dataset": "ref05_summary_forecast", "opcional": True},
            lake,
            "ref05_summary_forecast",
            "gold",
            {},
            AUSENTE,
        )
        is None
    )


@pytest.mark.parametrize("codigo", ["lake_escopo_obsoleto", "objeto_ausente", "lake_somente_local"])
async def test_so_dataset_ausente_passa_pela_tolerancia(codigo):
    """A scope stamped as obsolete, a refused credential, a lake guard: none of that is 'period that
    never closed', and swallowing them would hide an infrastructure failure behind an empty
    table."""
    lake = LakeFalso({"ref05_summary_forecast/_atual.json": _indice(["competencia"])})
    outro = ErroAplicacao(codigo=codigo, mensagem="another cause", status=400)
    assert (
        await leitura._escopo_opcional_vazio(
            {"dataset": "ref05_summary_forecast", "opcional": True},
            lake,
            "ref05_summary_forecast",
            "gold",
            {},
            outro,
        )
        is None
    )


async def test_validacao_aceita_o_booleano_e_recusa_o_texto():
    """A silently accepted config is worse than a refused one: `"true"` would let the source take
    the chain down at the first missing scope, which is what the flag exists to avoid."""
    from cflow_core.pipelines.definicao import validar

    def definicao(opcional):
        return {
            "nos": [
                {
                    "id": "hist",
                    "tipo": "fonte",
                    "config": {
                        "origem": "publicado",
                        "camada": "gold",
                        "dataset": "ref05_summary_forecast",
                        "opcional": opcional,
                    },
                },
                {"id": "s", "tipo": "sql", "sql": "SELECT * FROM {hist}"},
            ],
            "arestas": [{"de": "hist", "para": "s"}],
        }

    validar(definicao(True))  # does not raise
    with pytest.raises(ErroAplicacao) as e:
        validar(definicao("true"))
    assert "opcional" in str(e.value.detalhes[0].mensagem)


async def test_colunas_declaradas_toleram_o_dataset_que_nunca_foi_publicado():
    """The previous month in a lake where the summary dataset never published: with no index, the
    schema comes from `config.colunas` - this used to take down the whole closing chain."""
    vazio = await leitura._escopo_opcional_vazio(
        {"dataset": "summary/orders", "opcional": True, "colunas": ["order_id", "execucao_id"]},
        LakeFalso({}),
        "summary/orders",
        "gold",
        {"operacao_id": "1", "competencia": "2026-08-01"},
        AUSENTE,
    )
    assert vazio is not None and vazio["colunas"] == ["order_id", "execucao_id"]
    assert vazio["execucao_id"] is None


async def test_indice_vence_as_colunas_declaradas():
    lake = LakeFalso({"summary/orders/_atual.json": _indice(["order_id", "project_id"])})
    vazio = await leitura._escopo_opcional_vazio(
        {"dataset": "summary/orders", "opcional": True, "colunas": ["order_id"]},
        lake,
        "summary/orders",
        "gold",
        {},
        AUSENTE,
    )
    assert vazio["colunas"] == ["order_id", "project_id"]
