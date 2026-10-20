"""Local lake cache cap (`CFLOW_LAKE_CACHE_MAX_MIB`): deletes by oldest use, never the
just-downloaded file, the in-progress temp file or a recently used file."""

import os
import time
from pathlib import Path

import pytest

from cflow_core.config import get_settings
from cflow_core.ingestao import armazenamento as A

MIB = 1024 * 1024


def _arquivo(p: Path, mib: int, idade_s: float) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * (mib * MIB))
    t = time.time() - idade_s
    os.utime(p, (t, t))
    return p


def test_apaga_pelo_mais_antigo_ate_caber(tmp_path):
    a = _arquivo(tmp_path / "gold" / "a.parquet", 3, 3600)
    b = _arquivo(tmp_path / "gold" / "b.parquet", 3, 1800)
    c = _arquivo(tmp_path / "bronze" / "c.parquet", 3, 900)
    apagados = A.aparar_cache(tmp_path, 5 * MIB)
    assert apagados == 6 * MIB
    assert not a.exists() and not b.exists() and c.exists()


def test_preserva_recente_temporario_e_protegido(tmp_path):
    velho = _arquivo(tmp_path / "v.parquet", 3, 3600)
    protegido = _arquivo(tmp_path / "p.parquet", 3, 7200)
    recente = _arquivo(tmp_path / "r.parquet", 3, 60)
    parcial = _arquivo(tmp_path / ".x.parquet.abc.parcial", 3, 7200)
    A.aparar_cache(tmp_path, 1 * MIB, proteger=protegido)
    assert not velho.exists()
    assert protegido.exists() and recente.exists() and parcial.exists()


def test_abaixo_do_teto_nao_mexe(tmp_path):
    a = _arquivo(tmp_path / "a", 2, 3600)
    assert A.aparar_cache(tmp_path, 3 * MIB) == 0
    assert a.exists()


@pytest.fixture
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv("CFLOW_LAKE_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("CFLOW_LAKE_CACHE_MAX_MIB", "8")
    monkeypatch.setenv("CFLOW_LAKE_SOMENTE_LOCAL", "false")
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _gcs(monkeypatch, mib: int) -> A.ArmazenamentoGcs:
    async def _baixar(self, camada, caminho, tmp):
        Path(tmp).write_bytes(b"\1" * (mib * MIB))

    monkeypatch.setattr(A.ArmazenamentoGcs, "_baixar_em_blocos", _baixar)
    return A.ArmazenamentoGcs(buckets={"gold": "g"}, credencial={})


def _tamanho(raiz: Path) -> int:
    return sum(p.stat().st_size for p in raiz.rglob("*") if p.is_file())


async def test_downloads_sucessivos_ficam_no_teto(_cache, monkeypatch):
    """20 downloads of 2 MiB (40 MiB) with an 8 MiB cap: the cache ends at the cap, and without a cap
    it would end with all 40. Each downloaded file is aged so the trimmer can take it."""
    lake = _gcs(monkeypatch, 2)
    for i in range(20):
        destino = _cache / "gold" / f"ds{i}" / "dados.parquet"
        await lake.materializar("gold", f"ds{i}/dados.parquet", destino)
        assert destino.exists(), "the just-downloaded file never goes"
        t = time.time() - 3600
        os.utime(destino, (t, t))
    assert _tamanho(_cache) <= 8 * MIB


async def test_acerto_de_cache_renova_o_uso(_cache, monkeypatch):
    lake = _gcs(monkeypatch, 1)
    destino = _cache / "gold" / "a" / "dados.parquet"
    await lake.materializar("gold", "a/dados.parquet", destino)
    t = time.time() - 3600
    os.utime(destino, (t, t))
    await lake.materializar("gold", "a/dados.parquet", destino)
    assert time.time() - destino.stat().st_mtime < 60


async def test_sem_teto_nao_toca_nem_apaga(tmp_path, monkeypatch):
    monkeypatch.setenv("CFLOW_LAKE_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("CFLOW_LAKE_CACHE_MAX_MIB", raising=False)
    get_settings.cache_clear()
    try:
        lake = _gcs(monkeypatch, 2)
        for i in range(6):
            await lake.materializar("gold", f"d{i}", tmp_path / "gold" / f"d{i}")
        assert _tamanho(tmp_path) == 12 * MIB
    finally:
        get_settings.cache_clear()


def test_dois_aparos_simultaneos_nao_apagam_em_dobro(tmp_path):
    """Without the lock, both threads would add up the same total and each delete the excess."""
    import threading

    for i in range(10):
        _arquivo(tmp_path / f"f{i}", 1, 3600 - i)
    barreira = threading.Barrier(2)
    apagados = []

    def _aparar():
        barreira.wait()
        apagados.append(A.aparar_cache(tmp_path, 6 * MIB))

    ts = [threading.Thread(target=_aparar) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(apagados) == 4 * MIB
    assert len(list(tmp_path.iterdir())) == 6


def test_arquivo_com_hardlink_nao_e_candidato_e_o_disco_libera_o_dito(tmp_path):
    """The handoff snapshot is a hardlink of the cache: deleting the cache name does not return the
    space. The trimmer leaves that file out and what it says it deleted really leaves the disk."""
    cache, snaps = tmp_path / "cache", tmp_path / "snapshots"
    snaps.mkdir()
    ligado = _arquivo(cache / "ligado.parquet", 3, 7200)
    os.link(ligado, snaps / "no.parquet")
    livre = _arquivo(cache / "livre.parquet", 3, 3600)
    antes = os.statvfs(tmp_path)
    apagados = A.aparar_cache(cache, 1 * MIB)
    depois = os.statvfs(tmp_path)
    assert ligado.exists() and not livre.exists()
    assert apagados >= 3 * MIB
    liberado = (depois.f_bavail - antes.f_bavail) * depois.f_frsize
    if liberado == 0:
        pytest.skip(
            "this filesystem (e.g. Docker overlayfs) does not report freed space through statvfs"
        )
    assert liberado >= apagados * 0.9, (liberado, apagados)


def test_hardlink_nao_conta_no_teto_nem_esvazia_o_cache(tmp_path):
    """With the linked files (snapshot space) above the cap, the rest of the cache is not emptied."""
    cache, snaps = tmp_path / "cache", tmp_path / "snapshots"
    snaps.mkdir()
    for i in range(3):
        f = _arquivo(cache / f"ligado{i}.parquet", 3, 7200)
        os.link(f, snaps / f"n{i}.parquet")
    livre = _arquivo(cache / "livre.parquet", 2, 3600)
    assert A.aparar_cache(cache, 4 * MIB) == 0
    assert livre.exists()


def test_temporario_em_curso_conta_no_total(tmp_path):
    """The `.parcial` file occupies disk: it counts in the total (without being deleted) and the
    trimmer makes room around it."""
    _arquivo(tmp_path / ".x.parquet.abc.parcial", 4, 7200)
    velho = _arquivo(tmp_path / "velho.parquet", 3, 3600)
    A.aparar_cache(tmp_path, 5 * MIB)
    assert not velho.exists()
    assert (tmp_path / ".x.parquet.abc.parcial").exists()
