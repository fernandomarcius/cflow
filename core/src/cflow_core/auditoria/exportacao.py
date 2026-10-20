"""Export of the query result as CSV and Excel. Same columns as the trail; timestamps in the display timezone.

A missing side of the diff (creation/deletion) is rendered as an empty string, without breaking the file.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime

from ..timezone import para_exibicao

COLUNAS = [
    "ocorrido_em",
    "ator_id",
    "ator_tipo",
    "acao",
    "recurso_tipo",
    "recurso_id",
    "antes",
    "depois",
    "request_id",
    "ip",
]


def _valor(linha: dict, coluna: str) -> str:
    v = linha.get(coluna)
    if v is None:
        return ""
    if coluna == "ocorrido_em" and isinstance(v, datetime):
        return para_exibicao(v).isoformat()
    if coluna in ("antes", "depois") and isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def para_csv(linhas: list[dict]) -> str:
    """Build the CSV (with a header even when there are no rows)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUNAS)
    for linha in linhas:
        w.writerow([_valor(linha, c) for c in COLUNAS])
    return buf.getvalue()


def para_xlsx(linhas: list[dict]) -> bytes:
    """Build the XLSX (openpyxl). Same content as the CSV."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "auditoria"
    ws.append(COLUNAS)
    for linha in linhas:
        ws.append([_valor(linha, c) for c in COLUNAS])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
