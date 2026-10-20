"""The current name of a gold dataset - what keeps runs recorded before the switch readable."""

from __future__ import annotations

from cflow_core.ingestao.familia import nome_vigente, novo_nome


def test_nome_antigo_e_novo_sao_o_mesmo_dataset():
    """A version executed before the switch stores `ref01_summary`; the data lives at
    `ref/01/summary`. Without this equivalence, all earlier history would give `publicado_ausente`."""
    assert nome_vigente("ref01_summary") == nome_vigente("ref/01/summary") == "ref/01/summary"
    assert nome_vigente("load_orders") == "load/orders"
    assert nome_vigente("export_base_sales") == nome_vigente("base_sales") == "ref/export/sales"


def test_dataset_fora_das_familias_fica_como_esta():
    """Silver and datasets without a family must not be renamed by mistake."""
    assert nome_vigente("daily_rates") == "daily_rates"
    assert novo_nome("daily_rates") is None


def test_idempotente():
    assert novo_nome("ref/01/summary") is None
    assert nome_vigente(nome_vigente("ref01_summary")) == "ref/01/summary"
