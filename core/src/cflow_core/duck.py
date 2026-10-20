"""DuckDB connection with a BUDGET: memory, threads and spill directory per profile.

A bare `duckdb.connect()` assumes ~80 % of the machine RAM and all cores, PER CONNECTION. The
worker runs N activities at once, each with its own connection, inside a container with a limit:
N large nodes add up to N x 80 % and the container dies of OOM although no single node erred.
Here the ceiling comes from the container's own cgroup (Docker and Kubernetes both use cgroup v2)
and is divided by the number of simultaneous activities of the worker: N x limit <= container by
construction, and what does not fit spills to `temp_directory` instead of killing the process.

Threads come from `cpu.max` and, failing that, from the process AFFINITY (`cpuset` restricts the
cores without writing any quota; see `cpus_afinidade`).

`CFLOW_DUCKDB_MEMORY_LIMIT`/`CFLOW_DUCKDB_THREADS`/`CFLOW_DUCKDB_TEMP_DIR` override the derivation.
With no basis to decide (no cgroup, no affinity, no setting) the connection is left as it always was.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import get_settings

CGROUP_MEMORIA = Path("/sys/fs/cgroup/memory.max")
CGROUP_CPU = Path("/sys/fs/cgroup/cpu.max")
RESERVA_PROCESSO_MIB = 512  # left for Python, the Temporal SDK and the I/O buffer
PISO_MIB = 256

# How many connections of this profile can be open at once in the SAME process; it is the
# budget divisor. `execucao` and `bronze` run in the worker (one per activity); `preview` runs in
# the API, where the practical ceiling is the number of simultaneous editing sessions.
PERFIS = ("execucao", "preview", "bronze")


def _ler(caminho: Path) -> str | None:
    try:
        return caminho.read_text().strip()
    except OSError:
        return None


def limite_memoria_cgroup_mib(caminho: Path | None = None) -> int | None:
    """`memory.max` converted to MiB; None with no limit (`max`) or outside cgroup v2."""
    texto = _ler(caminho or CGROUP_MEMORIA)
    if not texto or texto == "max":
        return None
    try:
        return int(texto) // (1024 * 1024)
    except ValueError:
        return None


def cpus_cgroup(caminho: Path | None = None) -> float | None:
    """`cpu.max` = "<quota> <period>" or "max <period>"; returns quota/period, None with no ceiling."""
    texto = _ler(caminho or CGROUP_CPU)
    if not texto:
        return None
    partes = texto.split()
    if len(partes) != 2 or partes[0] == "max":
        return None
    try:
        return int(partes[0]) / int(partes[1])
    except (ValueError, ZeroDivisionError):
        return None


def cpus_afinidade() -> int | None:
    """How many CPUs this process can actually use, by AFFINITY; None where the OS lacks the
    call (macOS).

    This closes the `cpu.max` gap: `cpuset` (compose's `cpuset:`, a Kubernetes CPU node selector)
    restricts WHICH cores the process runs on without writing any quota to `cpu.max`. Without it
    DuckDB saw all the host CPUs and opened that many threads per instance although only a few
    cores were available, each instance building and tearing down its own pool."""
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return None


def tmpfs_de_trabalho_mib() -> int:
    """MiB of the tmpfs mounts hosting the working directories (snapshots, DuckDB spill). Zero
    outside Linux or when none of them is tmpfs.

    Why it exists: on Kubernetes `/tmp` may be a MEMORY-backed emptyDir counted in the SAME cgroup
    as the pod. Dividing the whole limit among the DuckDB instances while also writing gigabytes
    of snapshots to tmpfs promises the process more memory than it has; the outcome is the pod's
    OOM-kill with runs in flight, not a node error."""
    try:
        montagens = [
            (partes[1], partes[2])
            for partes in (linha.split() for linha in open("/proc/mounts", encoding="utf-8"))
            if len(partes) >= 3
        ]
    except OSError:
        return 0
    s = get_settings()
    vistos: set[str] = set()
    total = 0
    for diretorio in {s.snapshot_dir, s.duckdb_temp_dir, getattr(s, "lake_cache_dir", None)}:
        if not diretorio:
            continue
        caminho = os.path.realpath(diretorio)
        ponto, tipo = max(
            (
                (m, t)
                for m, t in montagens
                if caminho == m or caminho.startswith(m.rstrip("/") + "/")
            ),
            key=lambda mt: len(mt[0]),
            default=("", ""),
        )
        if tipo != "tmpfs" or ponto in vistos:
            continue
        vistos.add(ponto)
        try:
            st = os.statvfs(ponto)
        except OSError:
            continue
        total += (st.f_blocks * st.f_frsize) // (1024 * 1024)
    return total


def orcamento(perfil: str, *, divisor: int | None = None) -> dict[str, str | int]:
    """The pragmas to apply for this profile: `memory_limit` (DuckDB text), `threads`,
    `temp_directory`; only those there is a basis to decide."""
    if perfil not in PERFIS:
        raise ValueError(f"unknown DuckDB profile: {perfil}")
    s = get_settings()
    if divisor is None:
        divisor = 4 if perfil == "preview" else max(1, int(s.worker_max_atividades))
    out: dict[str, str | int] = {}
    if s.duckdb_memory_limit:
        out["memory_limit"] = s.duckdb_memory_limit
    else:
        total = limite_memoria_cgroup_mib()
        if total is not None:
            # The working tmpfs comes out of the total: what is written to it is memory of the same
            # cgroup. Capped at HALF the cgroup: an emptyDir without `sizeLimit` mounts with half
            # the NODE RAM (tens of GiB), and discounting it whole would push every instance to
            # the floor, spilling.
            util = total - RESERVA_PROCESSO_MIB - min(tmpfs_de_trabalho_mib(), total // 2)
            out["memory_limit"] = f"{max(PISO_MIB, util // divisor)}MB"
    if s.duckdb_threads:
        out["threads"] = int(s.duckdb_threads)
    else:
        # Quota first (a TIME ceiling, more restrictive); affinity after, which is a CORE ceiling
        # and exists in every container with `cpuset`. The floor is 2: with the default divisor
        # (8 activities) and 4 cores, the division gives 0, and one thread per instance serializes
        # `read_parquet` and `COPY`, which parallelize well inside a node.
        cpus = cpus_cgroup()
        if cpus is None:
            cpus = cpus_afinidade()
        if cpus is not None:
            out["threads"] = max(2, int(cpus // divisor))
    if s.duckdb_temp_dir:
        out["temp_directory"] = s.duckdb_temp_dir
    return out


def conectar(perfil: str = "execucao", *, divisor: int | None = None):
    """In-memory `duckdb.connect()` with the profile budget applied.

    The budget goes AT OPEN (`config=`), not in a later `SET`: the instance starts its thread pool
    on creation, and with a later `SET threads` it first created one thread per visible CPU and
    then tore them down; with one instance per node that adds up. Without a budget (no cgroup nor
    setting) the `config` is empty and the connection is exactly as before."""
    import duckdb

    return duckdb.connect(
        config={chave: str(valor) for chave, valor in orcamento(perfil, divisor=divisor).items()}
    )
