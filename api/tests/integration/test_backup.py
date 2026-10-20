"""Testable backup: the backup -> restore round trip preserves the data (audit trail included).

Needs `docker` and a running Postgres container (`CFLOW_PG_CONTAINER`, default
`cflow-postgres-1`); without them the test is skipped.
"""

from __future__ import annotations

import os
import subprocess
import uuid

import pytest
from sqlalchemy import text

from .conftest import DB_TESTE

pytestmark = pytest.mark.integration

CONTAINER = os.environ.get("CFLOW_PG_CONTAINER", "cflow-postgres-1")


def _container_no_ar() -> bool:
    try:
        r = subprocess.run(["docker", "exec", CONTAINER, "true"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _container_no_ar(), reason="Postgres container unavailable (docker)")
async def test_roundtrip_backup_restauracao(owner_engine):
    # POSTGRES_PASSWORD must be in the environment BEFORE the script is imported (docker exec)
    os.environ.setdefault("POSTGRES_PASSWORD", "change-me-owner")
    import backup  # scripts/backup.py (on pytest's pythonpath)

    # unique marker in the trail
    marcador = f"backup-{uuid.uuid4()}"
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO auditoria (ator_tipo, acao, recurso_tipo) VALUES ('sistema', :a, 'teste')"
            ),
            {"a": marcador},
        )

    r = backup.verificar_restauracao(db_origem=DB_TESTE, destino="cflow_backup_check_test")

    assert r["ok"] is True  # counts match between source and restored
    assert r["tabelas"]["auditoria"]["restaurado"] == r["tabelas"]["auditoria"]["origem"]
    assert r["tamanho_dump"] > 0
