"""The audit feature is read-only over the trail."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from cflow_api.main import app

pytestmark = pytest.mark.integration


def test_nenhuma_rota_de_auditoria_muta_a_trilha():
    for rota in app.routes:
        if not isinstance(rota, APIRoute):
            continue
        # routes under /auditoria can only be reads (GET)
        if rota.path.startswith("/auditoria"):
            assert rota.methods <= {"GET", "HEAD"}, f"{rota.path} exposes a mutating method"
