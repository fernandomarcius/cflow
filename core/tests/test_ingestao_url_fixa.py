"""FIXED-URL source: each capture is the WHOLE dataset, so reading all of them sums the same data N times.

Files like `registry.csv` are overwritten by the origin - it keeps no history. For these sources the
partition is the date of the **capture**, not of the data (`contrato.depende_da_fatia` is what
tells them apart: generating one slice per day would download TODAY's file several times and label
it with dates on which it did not exist).

`lotes_bronze` deduplicates by `chave_lote` - one version per slice, which is right for a series in
which each slice is a piece of time. Not here: the slices do not add up, they **repeat**.

Without the cut, republishing any of these sources would double the silver layer, and the ratio
would grow with every monthly reload.
"""

from __future__ import annotations

from cflow_core.ingestao.leitura import _so_a_captura_mais_recente

# Contract with a temporal placeholder in the URL: the request CHANGES with the period -> has history.
COM_HISTORICO = {"acesso": {"url_padrao": "https://example.invalid/report_{ano}{mes}.zip"}}
# URL with no placeholder: the origin overwrites a single file.
URL_FIXA = {"acesso": {"url_padrao": "https://example.invalid/registry.csv"}}


def _lotes(*chaves: str) -> list[dict]:
    return [{"chave_lote": k, "linhas": 100} for k in chaves]


def test_url_fixa_le_so_a_captura_mais_recente() -> None:
    fonte = {"contrato": URL_FIXA}
    lotes = _lotes("2026-08-15", "2026-09-01")
    assert [x["chave_lote"] for x in _so_a_captura_mais_recente(fonte, lotes)] == ["2026-09-01"]


def test_url_fixa_com_tres_capturas_continua_devolvendo_uma() -> None:
    """The ratio grows with every monthly reload - the cut cannot depend on there being two."""
    fonte = {"contrato": URL_FIXA}
    lotes = _lotes("2026-08-15", "2026-09-01", "2026-10-01")
    assert [x["chave_lote"] for x in _so_a_captura_mais_recente(fonte, lotes)] == ["2026-10-01"]


def test_fonte_com_historico_le_todas_as_fatias() -> None:
    """Most sources fall here: slices are pieces of time and they ADD UP. Cutting would lose data."""
    fonte = {"contrato": COM_HISTORICO}
    lotes = _lotes("2026-06", "2026-07", "2026-08")
    assert len(_so_a_captura_mais_recente(fonte, lotes)) == 3


def test_o_corte_respeita_o_recorte_pedido_em_vez_de_pular_para_hoje() -> None:
    """`chave_max` already cut earlier: the cut takes the LARGEST of what remains, not the current capture.

    This keeps the check repeatable for a past date - if the cut were "today's", every slice
    would always return the same capture and looking back would be impossible.
    """
    fonte = {"contrato": URL_FIXA}
    # What `lotes_bronze(chave_max='2026-08-31')` would have returned.
    lotes = _lotes("2026-07-15", "2026-08-15")
    assert [x["chave_lote"] for x in _so_a_captura_mais_recente(fonte, lotes)] == ["2026-08-15"]


def test_uma_captura_so_passa_intacta() -> None:
    for contrato in (URL_FIXA, COM_HISTORICO):
        lotes = _lotes("2026-09-01")
        assert len(_so_a_captura_mais_recente({"contrato": contrato}, lotes)) == 1


def test_lista_vazia_nao_quebra() -> None:
    """`resolver` raises `lake_sem_lotes` AFTER this cut - it must not blow up before."""
    assert _so_a_captura_mais_recente({"contrato": URL_FIXA}, []) == []


def test_contrato_ausente_nao_corta() -> None:
    """Without a contract there is no way to claim the origin has no history - cutting would lose data."""
    lotes = _lotes("2026-08", "2026-09")
    assert len(_so_a_captura_mais_recente({}, lotes)) == 2
