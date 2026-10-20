"""No API route edits or deletes audit rows."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from cflow_api.main import app

pytestmark = pytest.mark.integration

METODOS_MUTACAO = {"PUT", "PATCH", "DELETE"}


def test_nenhuma_rota_muta_auditoria():
    for rota in app.routes:
        if not isinstance(rota, APIRoute):
            continue
        caminho = rota.path.lower()
        if "auditoria" in caminho:
            # any audit route that exists must not mutate the trail
            assert not (rota.methods & METODOS_MUTACAO), (
                f"Route {rota.path} exposes a mutating method over the audit trail"
            )

    # the foundation must not expose ANY mutation route yet
    metodos_expostos = {
        m for rota in app.routes if isinstance(rota, APIRoute) for m in rota.methods
    }
    assert not (metodos_expostos & METODOS_MUTACAO), (
        f"The foundation should not expose mutation routes: {metodos_expostos & METODOS_MUTACAO}"
    )
