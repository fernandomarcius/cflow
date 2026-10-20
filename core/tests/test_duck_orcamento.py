"""Per-connection DuckDB budget: derived from the container cgroup and divided by the concurrent
activities -- N x limit <= container by construction. Threads come from `cpu.max` and, failing that,
from AFFINITY (`cpuset` restricts cores without writing a quota). With no basis to decide and no
setting, nothing changes."""

from __future__ import annotations

import pytest

from cflow_core import duck
from cflow_core.config import get_settings


@pytest.fixture(autouse=True)
def _settings_limpas(monkeypatch):
    for v in ("CFLOW_DUCKDB_MEMORY_LIMIT", "CFLOW_DUCKDB_THREADS", "CFLOW_DUCKDB_TEMP_DIR"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CFLOW_WORKER_MAX_ATIVIDADES", "8")
    # The REAL tmpfs of the machine running the tests would enter the budget; whoever wants to
    # exercise it declares it.
    monkeypatch.setattr(duck, "tmpfs_de_trabalho_mib", lambda: 0)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _sem_afinidade(monkeypatch):
    """The REAL process affinity would enter the budget and make the test measure the machine running
    it -- 1 thread in CI, 4 on a laptop. Whoever wants to exercise it declares it."""
    monkeypatch.setattr(duck, "cpus_afinidade", lambda: None)


def test_deriva_do_cgroup_dividindo_pelas_activities(tmp_path, monkeypatch, _sem_afinidade):
    (tmp_path / "memory.max").write_text(str(16 * 1024**3))  # 16 GiB
    (tmp_path / "cpu.max").write_text("3200000 100000")  # 32 cpus
    monkeypatch.setattr(duck, "CGROUP_MEMORIA", tmp_path / "memory.max")
    monkeypatch.setattr(duck, "CGROUP_CPU", tmp_path / "cpu.max")

    o = duck.orcamento("execucao")

    assert o["memory_limit"] == f"{(16 * 1024 - 512) // 8}MB"  # (16 GiB - reserve) / 8
    assert o["threads"] == 4  # 32 cpus / 8 activities


def test_piso_de_duas_threads(tmp_path, monkeypatch, _sem_afinidade):
    """8 activities on 4 cores gives 0 in the division. One thread per instance serializes
    `read_parquet` and `COPY`, which are what parallelize well INSIDE a node."""
    (tmp_path / "cpu.max").write_text("400000 100000")  # 4 cpus
    monkeypatch.setattr(duck, "CGROUP_CPU", tmp_path / "cpu.max")
    monkeypatch.setattr(duck, "CGROUP_MEMORIA", tmp_path / "nao-existe")

    assert duck.orcamento("execucao")["threads"] == 2


def test_afinidade_entra_quando_o_cgroup_nao_tem_quota(tmp_path, monkeypatch):
    """`cpuset` (the compose `cpuset:`) restricts the CORES without writing a quota in `cpu.max`.
    Without this DuckDB opened one thread per host CPU, not per available CPU."""
    (tmp_path / "cpu.max").write_text("max 100000")
    monkeypatch.setattr(duck, "CGROUP_CPU", tmp_path / "cpu.max")
    monkeypatch.setattr(duck, "CGROUP_MEMORIA", tmp_path / "nao-existe")
    monkeypatch.setattr(duck, "cpus_afinidade", lambda: 32)

    assert duck.orcamento("execucao")["threads"] == 4  # 32 / 8 activities


def test_quota_do_cgroup_vence_a_afinidade(tmp_path, monkeypatch):
    """The quota is a TIME ceiling and is more restrictive than the core set."""
    (tmp_path / "cpu.max").write_text("1600000 100000")  # 16 cpus
    monkeypatch.setattr(duck, "CGROUP_CPU", tmp_path / "cpu.max")
    monkeypatch.setattr(duck, "CGROUP_MEMORIA", tmp_path / "nao-existe")
    monkeypatch.setattr(duck, "cpus_afinidade", lambda: 64)

    assert duck.orcamento("execucao")["threads"] == 2  # 16 / 8, not 64 / 8


def test_sem_limite_no_cgroup_e_sem_afinidade_nao_impoe_nada(tmp_path, monkeypatch, _sem_afinidade):
    (tmp_path / "memory.max").write_text("max")
    (tmp_path / "cpu.max").write_text("max 100000")
    monkeypatch.setattr(duck, "CGROUP_MEMORIA", tmp_path / "memory.max")
    monkeypatch.setattr(duck, "CGROUP_CPU", tmp_path / "cpu.max")

    assert duck.orcamento("execucao") == {}


def test_fora_de_cgroup_e_sem_afinidade_nao_impoe_nada(tmp_path, monkeypatch, _sem_afinidade):
    monkeypatch.setattr(duck, "CGROUP_MEMORIA", tmp_path / "nao-existe")
    monkeypatch.setattr(duck, "CGROUP_CPU", tmp_path / "nao-existe")
    assert duck.orcamento("execucao") == {}


def test_setting_explicita_vence_o_cgroup(tmp_path, monkeypatch):
    (tmp_path / "memory.max").write_text(str(16 * 1024**3))
    monkeypatch.setattr(duck, "CGROUP_MEMORIA", tmp_path / "memory.max")
    monkeypatch.setenv("CFLOW_DUCKDB_MEMORY_LIMIT", "3GB")
    monkeypatch.setenv("CFLOW_DUCKDB_THREADS", "2")
    monkeypatch.setenv("CFLOW_DUCKDB_TEMP_DIR", str(tmp_path))
    get_settings.cache_clear()

    o = duck.orcamento("execucao")
    assert o == {"memory_limit": "3GB", "threads": 2, "temp_directory": str(tmp_path)}


def test_piso_de_memoria_num_container_pequeno(tmp_path, monkeypatch):
    (tmp_path / "memory.max").write_text(str(1024**3))  # 1 GiB: (1024-512)/8 = 64 < floor
    monkeypatch.setattr(duck, "CGROUP_MEMORIA", tmp_path / "memory.max")
    assert duck.orcamento("execucao")["memory_limit"] == f"{duck.PISO_MIB}MB"


def test_conectar_aplica_o_orcamento(tmp_path, monkeypatch):
    monkeypatch.setenv("CFLOW_DUCKDB_MEMORY_LIMIT", "300MB")
    monkeypatch.setenv("CFLOW_DUCKDB_THREADS", "1")
    get_settings.cache_clear()
    con = duck.conectar("execucao")
    valor = con.execute("SELECT current_setting('threads')").fetchone()[0]
    assert str(valor) == "1"
    mem = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    assert "MB" in str(mem) or "MiB" in str(mem)


def test_perfil_desconhecido_e_erro():
    with pytest.raises(ValueError):
        duck.orcamento("qualquer")


def test_tmpfs_de_trabalho_sai_do_orcamento(tmp_path, monkeypatch, _sem_afinidade):
    """When `/tmp` is an in-memory emptyDir in the SAME cgroup: 8 x (16 GiB - reserve) / 8 plus 5 GiB
    of snapshot exceeds the pod limit. What the tmpfs may occupy comes off before the division."""
    (tmp_path / "memory.max").write_text(str(16 * 1024**3))
    monkeypatch.setattr(duck, "CGROUP_MEMORIA", tmp_path / "memory.max")
    monkeypatch.setattr(duck, "CGROUP_CPU", tmp_path / "nao-existe")
    monkeypatch.setattr(duck, "tmpfs_de_trabalho_mib", lambda: 5 * 1024)

    assert duck.orcamento("execucao")["memory_limit"] == f"{(16 * 1024 - 512 - 5 * 1024) // 8}MB"


def test_tmpfs_de_trabalho_le_o_ponto_de_montagem_do_diretorio(tmp_path, monkeypatch):
    """Only counts the tmpfs where the working directories LIVE (the longest mount that contains
    them), and each mount only once."""
    monkeypatch.undo()  # without the fixture's stub: here it is the real function
    montagens = tmp_path / "mounts"
    montagens.write_text(
        "overlay / overlay rw 0 0\n"
        f"tmpfs {tmp_path} tmpfs rw,size=65536k 0 0\n"
        "tmpfs /dev/shm tmpfs rw 0 0\n"
    )
    snap = tmp_path / "snap"
    snap.mkdir()
    monkeypatch.setenv("CFLOW_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("CFLOW_DUCKDB_TEMP_DIR", str(tmp_path / "spill"))
    get_settings.cache_clear()
    aberto = open

    def _open(caminho, *a, **k):
        return aberto(montagens if caminho == "/proc/mounts" else caminho, *a, **k)

    monkeypatch.setattr("builtins.open", _open)

    class _St:
        f_blocks, f_frsize = 16, 1024 * 1024  # 16 MiB

    monkeypatch.setattr(duck.os, "statvfs", lambda _p: _St)
    assert duck.tmpfs_de_trabalho_mib() == 16
    get_settings.cache_clear()


def test_tmpfs_do_cache_do_lake_tambem_conta(tmp_path, monkeypatch):
    """The lake cache (datasets downloaded from object storage) lives on the same kind of volume: if
    it is another tmpfs, it is also cgroup memory and is deducted -- once per mount."""
    monkeypatch.undo()
    snap, cache = tmp_path / "snap", tmp_path / "cache"
    snap.mkdir()
    cache.mkdir()
    montagens = tmp_path / "mounts"
    montagens.write_text(f"tmpfs {snap} tmpfs rw 0 0\ntmpfs {cache} tmpfs rw 0 0\n")
    monkeypatch.setenv("CFLOW_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("CFLOW_LAKE_CACHE_DIR", str(cache))
    monkeypatch.delenv("CFLOW_DUCKDB_TEMP_DIR", raising=False)
    get_settings.cache_clear()
    aberto = open
    monkeypatch.setattr(
        "builtins.open",
        lambda c, *a, **k: aberto(montagens if c == "/proc/mounts" else c, *a, **k),
    )

    class _St:
        f_blocks, f_frsize = 8, 1024 * 1024

    monkeypatch.setattr(duck.os, "statvfs", lambda _p: _St)
    assert duck.tmpfs_de_trabalho_mib() == 16
    get_settings.cache_clear()
