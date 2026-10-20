"""Where the API integration suite finds what is not its own: the repository root and the worker.

The suite migrates the database with `migrations/`, exercises operations scripts (`scripts/`) and
the core fixtures (`core/tests/...`), all at the root of this repository. It also starts the WORKER
inside pytest.  Both paths are resolved here, once, instead of every test doing
`Path(__file__).parents[3]`.

Root (`raiz_plataforma`): `CFLOW_PLATAFORMA_DIR`, otherwise three levels above this file. If
neither is a checkout the error is LOUD: skipping the suite for lack of migrations would be an
"empty green". Worker (`dir_worker`): see the function.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_AQUI = Path(__file__).resolve()


def _eh_plataforma(caminho: Path) -> bool:
    return (caminho / "migrations" / "alembic.ini").is_file()


def raiz_plataforma() -> Path:
    explicito = os.environ.get("CFLOW_PLATAFORMA_DIR")
    if explicito:
        caminho = Path(explicito).expanduser().resolve()
        if not _eh_plataforma(caminho):
            raise RuntimeError(
                f"CFLOW_PLATAFORMA_DIR={explicito} is not a cFlow checkout "
                "(migrations/alembic.ini is missing)."
            )
        return caminho
    caminho = _AQUI.parents[3]
    if _eh_plataforma(caminho):
        return caminho
    raise RuntimeError(
        "cFlow checkout not found: set CFLOW_PLATAFORMA_DIR to the repository root. "
        f"Looked in: {caminho}"
    )


RAIZ_PLATAFORMA = raiz_plataforma()


def dir_worker() -> Path | None:
    """Checkout of the worker: the suite starts the worker INSIDE pytest to exercise execution
    through Temporal. `CFLOW_WORKER_DIR` (CI sets it), otherwise `worker/` at the repository root.
    Neither -> None, and the harness refuses loudly when it needs it
    (`conftest._worker_temporal`)."""
    explicito = os.environ.get("CFLOW_WORKER_DIR")
    candidatos = [Path(explicito).expanduser()] if explicito else [RAIZ_PLATAFORMA / "worker"]
    for caminho in candidatos:
        if (caminho / "src" / "cflow_worker").is_dir():
            return caminho.resolve()
    return None


DIR_WORKER = dir_worker()
if DIR_WORKER is not None and str(DIR_WORKER / "src") not in sys.path:
    sys.path.append(str(DIR_WORKER / "src"))
