"""Snapshot purge: removes the old ones while preserving each pipeline's latest run."""

from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path

import pytest

from cflow_core.config import get_settings
from cflow_core.execucao.manutencao import expurgar_snapshots, expurgar_snapshots_orfaos

from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao

pytestmark = pytest.mark.integration


async def test_expurgo_mantem_ultima(owner_engine, app_engine, cliente):
    _, h = await usuario_logado(
        owner_engine, cliente, email="exp@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "exp"}, headers=h)).json()["id"]
    definicao = {"nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}], "arestas": []}
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )

    e1 = (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()["id"]
    await aguardar_execucao(cliente, e1, h)
    e2 = (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()["id"]
    await aguardar_execucao(cliente, e2, h)
    base = get_settings().snapshot_dir
    assert os.path.isdir(f"{base}/{e1}") and os.path.isdir(f"{base}/{e2}")

    r = await expurgar_snapshots(
        manter_dias=0
    )  # everything is "old"; only each pipeline's latest run stays
    assert r["removidos"] >= 1
    assert not os.path.isdir(f"{base}/{e1}")  # old run: snapshot removed
    assert os.path.isdir(f"{base}/{e2}")  # the pipeline's latest run: preserved


def _dir_antigo(base: Path, nome: str, *, dias: int = 3, tamanho: int = 128) -> Path:
    """Create a snapshot directory with an mtime in the past (simulates leftovers from months ago)."""
    d = base / nome
    d.mkdir(parents=True, exist_ok=True)
    (d / "n1.parquet").write_bytes(b"x" * tamanho)
    antigo = time.time() - dias * 86400
    os.utime(d / "n1.parquet", (antigo, antigo))
    os.utime(d, (antigo, antigo))
    return d


@pytest.fixture
def snapshots_isolados(tmp_path, monkeypatch):
    """Point `snapshot_dir` at a temporary directory used ONLY by this test.

    This is not abstract caution: run against the development `snapshots/` with the `cflow_test`
    database (empty, truncated between tests), the routine classified everything as orphaned and
    deleted thousands of real directories. `snapshot_dir` is global and the test database is a
    different one; they never match. `get_settings` is an `lru_cache`, so swapping the attribute on
    the cached object reaches everyone (the engine included) and the monkeypatch undoes it at the
    end.
    """
    destino = tmp_path / "snapshots"
    destino.mkdir()
    monkeypatch.setattr(get_settings(), "snapshot_dir", str(destino))
    return destino


async def test_expurgo_orfaos_remove_so_o_que_sumiu_do_banco(
    owner_engine, cliente, snapshots_isolados
):
    """A run directory that no longer exists in `execucoes` goes away; the rest is left untouched.

    The real case: deleting a pipeline does `DELETE FROM execucoes` and leaves the Parquet files
    behind, accumulating gigabytes on a production-like host. The negative assertions matter as
    much as the positive one: this routine deletes directories on its own and must not touch a
    neighbour.
    """
    _, h = await usuario_logado(
        owner_engine, cliente, email="orf@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "orf"}, headers=h)).json()["id"]
    definicao = {"nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}], "arestas": []}
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    viva = (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()["id"]
    await aguardar_execucao(cliente, viva, h)

    base = snapshots_isolados
    orfao_velho = _dir_antigo(base, str(uuid.uuid4()))
    orfao_recente = base / str(uuid.uuid4())  # orphan, but created just now
    orfao_recente.mkdir(parents=True, exist_ok=True)
    (orfao_recente / "n1.parquet").write_bytes(b"y")
    nao_e_uuid = _dir_antigo(base, "cache-de-outra-coisa")

    r = await expurgar_snapshots_orfaos(idade_minima_dias=1)

    assert not orfao_velho.exists()  # gone from the database and the directory is old -> removed
    assert orfao_recente.exists()  # race guard: a recent one is NEVER deleted
    assert nao_e_uuid.exists()  # a non-conforming name is not ours -> left alone
    assert (base / viva).is_dir()  # live run: untouched
    assert r["removidos"] == 1 and r["bytes"] == 128 and r["ignorados"] >= 1


async def test_expurgo_orfaos_recusa_quando_o_banco_nao_tem_execucao(snapshots_isolados):
    """A database with no runs at all: the routine does NOT delete, which is how thousands of
    directories were once lost.

    With no run to compare against, "orphan" loses its meaning. The right answer is to do nothing
    and say why, not to read emptiness as permission to clean everything.
    """
    velho = _dir_antigo(snapshots_isolados, str(uuid.uuid4()))

    r = await expurgar_snapshots_orfaos(idade_minima_dias=1)

    assert velho.exists()
    assert r["removidos"] == 0 and "has no runs" in r["recusado"]
    assert r["candidatos"] == 1  # saw the candidate and refused anyway


async def test_expurgo_orfaos_recusa_quando_quase_tudo_parece_orfao(
    owner_engine, cliente, snapshots_isolados
):
    """A database with runs, but almost no directory matching: smells like another environment's
    database.

    Covers the case the "empty database" guard does not catch: a partial restore, another
    environment's database, `snapshot_dir` pointing at the wrong volume. A legitimate cleanup
    removes a part; removing almost everything is a diagnosis, not a cleanup.
    """
    _, h = await usuario_logado(
        owner_engine, cliente, email="orf2@example.com", perfil="especialista"
    )
    pid = (await cliente.post("/pipelines", json={"nome": "orf2"}, headers=h)).json()["id"]
    definicao = {"nos": [{"id": "n1", "tipo": "sql", "sql": "SELECT 1 AS a"}], "arestas": []}
    await cliente.put(
        f"/pipelines/{pid}/rascunho", json={"definicao": definicao, "base_numero": 0}, headers=h
    )
    await cliente.post(
        f"/pipelines/{pid}/publicar", json={"base_numero": 1, "nota": "v1"}, headers=h
    )
    viva = (await cliente.post(f"/pipelines/{pid}/executar", headers=h)).json()["id"]
    await aguardar_execucao(cliente, viva, h)
    shutil.rmtree(snapshots_isolados / viva)  # the only one that would match is not on disk

    velhos = [_dir_antigo(snapshots_isolados, str(uuid.uuid4())) for _ in range(20)]

    r = await expurgar_snapshots_orfaos(idade_minima_dias=1)
    assert r["removidos"] == 0 and "safety limit" in r["recusado"]
    assert all(d.exists() for d in velhos)

    # With `forcar=True` the operator explicitly takes the risk and the cleanup happens.
    r2 = await expurgar_snapshots_orfaos(idade_minima_dias=1, forcar=True)
    assert r2["removidos"] == 20 and not any(d.exists() for d in velhos)
