"""The `publicado` source must name the SAME scope that publishing named.

Writing (`engine._particao_de_parametro`) already resolved `competencia` through the window start;
reading (`leitura._valor`) did not. While no dataset partitioned by competence under the raw name,
nobody noticed - readers always read through `:competencia_m1`, a parameter that exists under that
name. On the first run after `competencia` entered the partition, EVERY read came out with an empty
`competencia=` prefix and failed with `lake_dataset_ausente`.
"""

from __future__ import annotations

from cflow_core.ingestao.leitura import _valor, competencia_expandida

# How normalisation delivers the parameters of a run: `competencia` does not exist under the raw
# name - it is expanded on both sides of the window.
NORMALIZADOS = {
    "operacao_id": "11",
    "data_registro": "2026-09-21",
    "competencia_inicio": "2026-08-01",
    "competencia_fim": "2026-09-01",
}


def test_competencia_crua_resolve_pelo_INICIO_da_janela():
    assert _valor(":competencia", NORMALIZADOS) == "2026-08-01"


def test_o_fim_da_janela_nunca_e_o_escopo():
    """`competencia_fim` is the FOLLOWING month. Resolving by it would shift every scope by a month."""
    assert _valor(":competencia", NORMALIZADOS) != NORMALIZADOS["competencia_fim"]


def test_parametro_com_nome_cru_presente_vence():
    """Whoever already sends a raw `competencia` (an old dispatch) keeps working - without that the
    fix would swap a working path for another."""
    assert _valor(":competencia", {**NORMALIZADOS, "competencia": "2026-07-01"}) == "2026-07-01"


def test_os_demais_parametros_nao_mudam():
    assert _valor(":operacao_id", NORMALIZADOS) == "11"
    assert _valor(":data_registro", NORMALIZADOS) == "2026-09-21"
    assert _valor("literal", NORMALIZADOS) == "literal"


def test_parametro_ausente_continua_sem_corte():
    """ "Blank = everything" is the node rule; here it becomes "no scope", and the caller decides."""
    assert _valor(":nao_existe", NORMALIZADOS) is None
    assert _valor(":competencia", {}) is None


def test_texto_opcional_em_branco_NAO_herda_de_um_vizinho_homonimo():
    """Without the type map, the expansion signature (BOTH sides) decides. A `texto` named
    `janela` with a neighbour `janela_inicio` must not inherit its value."""
    assert not competencia_expandida("janela", {"janela_inicio": "2026-08-01"})
    assert competencia_expandida("janela", {"janela_inicio": "x", "janela_fim": "y"})


def test_com_o_mapa_de_tipos_so_o_TIPO_decide():
    tipos = {"janela": "texto", "competencia": "competencia"}
    ps = {"janela_inicio": "a", "janela_fim": "b", "competencia_inicio": "2026-08-01"}
    assert not competencia_expandida("janela", ps, tipos)
    assert competencia_expandida("competencia", ps, tipos)
