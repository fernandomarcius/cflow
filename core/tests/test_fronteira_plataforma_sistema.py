"""The platform packages do not import application-specific packages.

A package that builds pipeline definitions for one specific application must stay outside the
platform runtime: it publishes through the API, where the definition becomes versioned JSON. If
`cflow_core` or `cflow_api` started importing such a package, the rules of ONE application would
live in the platform runtime again, with no error at all (the import would just work).

The test reads the SOURCE and imports nothing: a late import inside a function counts too.
"""

from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
# The worker lives in its own package and carries the same guard in its own tests.
PACOTES_DA_PLATAFORMA = ("core/src", "api/src")
PACOTES_DE_SISTEMA = ("app_pipelines",)
_IMPORT = re.compile(r"^\s*(?:from|import)\s+(" + "|".join(PACOTES_DE_SISTEMA) + r")\b", re.M)


def test_plataforma_nao_importa_pacote_de_sistema():
    achados = []
    for pacote in PACOTES_DA_PLATAFORMA:
        for arquivo in sorted((RAIZ / pacote).rglob("*.py")):
            for m in _IMPORT.finditer(arquivo.read_text(encoding="utf-8")):
                achados.append(f"{arquivo.relative_to(RAIZ)}: {m.group(0).strip()}")
    assert not achados, "the platform imports an application-specific package:\n" + "\n".join(
        achados
    )


def test_camada_de_relatorios_guarda_so_a_mecanica():
    """What lives in `cflow_core/relatorios/` is the generic LAYER: layout, generation and service.

    The list is explicit on purpose: a new module there must belong to the platform, and the
    decision shows up in this test's diff instead of slipping through, which is how
    application-specific logic tends to creep into the platform package, one module at a time."""
    pasta = RAIZ / "core" / "src" / "cflow_core" / "relatorios"
    # `celulas_xlsx` (data rows without a per-cell object) and `processo_xlsx` (generation outside
    # the nodes' GIL) are generation mechanics: no application rule, no layout data.
    assert sorted(p.name for p in pasta.glob("*.py")) == [
        "__init__.py",
        "celulas_xlsx.py",
        "gerar.py",
        "layout.py",
        "processo_xlsx.py",
        "service.py",
    ]
    assert not list(pasta.glob("*.json")), (
        "application data (measured layouts etc.) lives in its own package"
    )
