"""Dataset names in the gold FAMILY layout (`load/`, `ref/NN/`, `ref/export/`).

The gold layer was reorganised from flat names (`ref01_summary`) into families
(`ref/01/summary`). The rule lives here, and not only in a migration script, because the platform
still needs it afterwards: every run from BEFORE the switch stored the old name in the `saida`
node of the version that ran, and per-run reads (`publicados.saida_publicada`) find the node
through that version. Without treating both names as the same dataset, every run recorded before
the switch would become unreadable.
"""

from __future__ import annotations

import re

_LOAD = re.compile(r"^load_(?P<resto>.+)$")
_REF = re.compile(r"^ref(?P<num>\d{2})_(?P<resto>.+)$")
_EXPORT = re.compile(r"^(?:export_)?base_(?P<resto>.+)$")


def novo_nome(dataset: str | None) -> str | None:
    """The family name, or None when the dataset is not from a known family or already is in one.

    Idempotent: a dataset that already contains `/` returns None, so running twice never repacks.
    """
    if not dataset or "/" in dataset:
        return None
    if m := _LOAD.match(dataset):
        return f"load/{m['resto']}"
    if m := _REF.match(dataset):
        return f"ref/{m['num']}/{m['resto']}"
    if m := _EXPORT.match(dataset):
        return f"ref/export/{m['resto']}"
    return None


def nome_vigente(dataset: str | None) -> str | None:
    """Where the dataset lives TODAY: the family name when there is one, else its own name."""
    return novo_nome(dataset) or dataset
