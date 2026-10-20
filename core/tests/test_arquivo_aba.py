"""File connector: sheet selector (structured Excel) + full read (limit None). No database."""

from __future__ import annotations

import openpyxl
import pytest

from cflow_core.conexoes.conectores.arquivo import _ler


def _xlsx(path: str) -> None:
    wb = openpyxl.Workbook()
    s1 = wb.active
    s1.title = "One"
    s1.append(["a", "b"])
    s1.append([1, 2])
    s2 = wb.create_sheet("Two")
    s2.append(["x"])
    s2.append([9])
    s2.append([10])
    wb.save(path)


def test_aba_ativa_por_padrao(tmp_path):
    p = str(tmp_path / "m.xlsx")
    _xlsx(p)
    cols, linhas = _ler({"caminho": p, "formato": "xlsx"}, None)
    assert cols == ["a", "b"]  # header from the 1st row
    assert linhas == [{"a": 1, "b": 2}]


def test_aba_especifica_le_tudo(tmp_path):
    p = str(tmp_path / "m.xlsx")
    _xlsx(p)
    # sheet "Two" + limit None (execution) reads every row (regression: i >= None)
    cols, linhas = _ler({"caminho": p, "formato": "xlsx", "aba": "Two"}, None)
    assert cols == ["x"]
    assert linhas == [{"x": 9}, {"x": 10}]


def test_aba_inexistente_erro_claro(tmp_path):
    p = str(tmp_path / "m.xlsx")
    _xlsx(p)
    with pytest.raises(ValueError, match="not found"):
        _ler({"caminho": p, "formato": "xlsx", "aba": "Missing"}, None)
