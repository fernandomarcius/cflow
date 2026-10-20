"""Upload of source files (CSV/Excel/Parquet) and consumption by the 'arquivo' connector.

The upload directory is redirected to a temp dir (host) via CFLOW_UPLOAD_DIR + cache_clear, so
the test can run outside the container (where /app/uploads does not exist).
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest
from sqlalchemy import text

from cflow_core.conexoes import service as cs
from cflow_core.config import get_settings

from ._arnes_auth import usuario_logado

pytestmark = pytest.mark.integration

# Redirect the upload directory to a host temp dir BEFORE Settings is instantiated.
_DIR_UPLOAD = tempfile.mkdtemp(prefix="cflow-uploads-")
os.environ["CFLOW_UPLOAD_DIR"] = _DIR_UPLOAD
get_settings.cache_clear()


async def test_upload_csv_e_consumo_pelo_conector(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="up1@example.com", perfil="especialista"
    )

    r = await cliente.post(
        "/arquivos",
        files={"file": ("dados.csv", b"valor\n10\n20\n", "text/csv")},
        headers=h,
    )
    assert r.status_code == 200, r.text
    corpo = r.json()
    caminho = corpo["caminho"]
    assert corpo["nome_original"] == "dados.csv"
    assert corpo["tamanho"] == len(b"valor\n10\n20\n")
    # actually written to the upload directory, with an opaque name (not the client's name)
    assert os.path.isfile(caminho)
    assert caminho.startswith(_DIR_UPLOAD)
    assert os.path.basename(caminho) != "dados.csv"

    # create an 'arquivo' connection pointing at the path and read the uploaded content
    cid = (
        await cliente.post(
            "/conexoes",
            json={"nome": "arq-uploaded", "tipo": "arquivo", "config": {"caminho": caminho}},
            headers=h,
        )
    ).json()["id"]
    assert (await cliente.post(f"/conexoes/{cid}/testar", headers=h)).json()["ok"] is True
    async with app_engine.begin():
        _cols, linhas = await cs.amostra(uuid.UUID(cid), "", 10)
    assert len(linhas) == 2 and linhas[0]["valor"] == "10" and linhas[1]["valor"] == "20"


async def test_upload_grava_auditoria_sem_conteudo(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="up2@example.com", perfil="especialista"
    )
    r = await cliente.post(
        "/arquivos",
        files={"file": ("invoices.csv", b"a,b\n1,2\n", "text/csv")},
        headers=h,
    )
    assert r.status_code == 200, r.text

    async with owner_engine.begin() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT acao, recurso_tipo, depois FROM auditoria "
                        "WHERE recurso_tipo = 'arquivo' ORDER BY id DESC LIMIT 1"
                    )
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    assert row["acao"] == "criar"
    depois = row["depois"]
    assert depois["nome_original"] == "invoices.csv"
    assert "caminho" in depois and depois["tamanho"] == len(b"a,b\n1,2\n")
    # the file content NEVER shows up in the trail
    assert "conteudo" not in depois
    assert "1,2" not in str(depois)


@pytest.mark.parametrize("perfil", ["operador", "auditor"])
async def test_upload_negado_para_nao_especialista(owner_engine, app_engine, cliente, perfil):
    _, h = await usuario_logado(
        owner_engine, cliente, email=f"up-{perfil}@example.com", perfil=perfil
    )
    r = await cliente.post(
        "/arquivos",
        files={"file": ("dados.csv", b"valor\n1\n", "text/csv")},
        headers=h,
    )
    assert r.status_code == 403, r.text
    assert r.json()["erro"]["codigo"] == "nao_autorizado"


async def test_upload_formato_nao_suportado(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="up3@example.com", perfil="especialista"
    )
    r = await cliente.post(
        "/arquivos",
        files={"file": ("malware.exe", b"MZ\x90\x00", "application/octet-stream")},
        headers=h,
    )
    assert r.status_code == 400, r.text
    assert r.json()["erro"]["codigo"] == "formato_nao_suportado"
