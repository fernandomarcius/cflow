"""Sandboxed execution of a `python` node - the generic procedural path (what SQL cannot cover
goes to a Python node).

The node logic does NOT live in the engine: it lives in the DEFINITION (`config.codigo`,
versioned). The engine only runs the script in ISOLATION. Generic: fits any procedural calculation.

Isolation (air-gapped; deterministic by contract):
- separate Python **subprocess** (`-I`, isolated mode: ignores env/PYTHONPATH/user-site).
- **no network**: the runner blocks `socket` before the user code (a runtime barrier; strong
  network isolation belongs to the container/host, with egress closed).
- **limits** (POSIX `setrlimit`): CPU, memory (RLIMIT_AS), file size (RLIMIT_FSIZE); subprocess
  `timeout`. **Minimal env** (no credentials).
- **Parquet I/O**: inputs (predecessor snapshots) and output are files; the subprocess does not
  share the engine's DuckDB connection.

Contract of `config.codigo` (the user script):
- receives globals: `con` (DuckDB with each predecessor as a VIEW named by its node id),
  `entradas` (dict name -> parquet path), `parametros` (dict).
- must define **`resultado`**: a DuckDB relation (`con.sql(...)`) OR a list of dicts.

Determinism: the script MUST be a pure function of (inputs, parameters); this enables incremental
reuse (fingerprint by code + content). Using clocks or randomness breaks that guarantee.
"""

from __future__ import annotations

import json
import resource
import subprocess
import sys
import tempfile
from pathlib import Path

from ..config import get_settings
from ..errors import ErroAplicacao

# Runner: runs INSIDE the isolated subprocess. Reads the manifest, registers the inputs as VIEWs,
# executes the user code in a controlled namespace and writes `resultado` to Parquet.
_RUNNER = r"""
import json, sys, duckdb

mani = json.loads(open(sys.argv[1], encoding="utf-8").read())

# --- network block (runtime barrier; strong isolation = air-gapped container) ---
import socket
def _sem_rede(*a, **k):
    raise RuntimeError("network access blocked in the python node (sandbox)")
socket.socket = _sem_rede
socket.create_connection = _sem_rede
try:
    socket.socketpair = _sem_rede
except Exception:
    pass

con = duckdb.connect()
con.execute("SET memory_limit=?", [mani["memoria_duckdb"]])
entradas = mani["entradas"]
for nome, caminho in entradas.items():
    # CREATE VIEW does not accept prepared parameters -> inline path (engine-internal temp path).
    nn = nome.replace('"', '""'); cc = caminho.replace("'", "''")
    con.execute("CREATE VIEW \"%s\" AS SELECT * FROM read_parquet('%s')" % (nn, cc))
parametros = mani["parametros"]

ns = {"con": con, "entradas": entradas, "parametros": parametros}
codigo = open(mani["codigo_path"], encoding="utf-8").read()
exec(compile(codigo, "<no_python>", "exec"), ns)

resultado = ns.get("resultado")
saida = mani["saida"]
if resultado is None:
    raise RuntimeError("the script must define `resultado` (DuckDB relation or list of dicts)")
if isinstance(resultado, list):
    if not resultado:
        con.execute('CREATE TABLE _saida (vazio INTEGER)')
    else:
        tmp = saida + ".rows.json"
        with open(tmp, "w", encoding="utf-8") as _f:
            _f.write(json.dumps(resultado, ensure_ascii=False, default=str))
        con.execute("CREATE TABLE _saida AS SELECT * FROM read_json(?, format='array')", [tmp])
    con.execute("COPY _saida TO ? (FORMAT parquet)", [saida])
elif hasattr(resultado, "write_parquet"):       # DuckDBPyRelation
    resultado.write_parquet(saida)
else:
    raise RuntimeError("`resultado` must be a DuckDB relation (con.sql(...)) or a list of dicts")
"""


def _limites(memoria_bytes: int, fsize_bytes: int):
    """preexec_fn (POSIX): apply resource limits BEFORE the subprocess runs the code."""
    s = get_settings()
    cpu = max(1, int(s.sandbox_timeout_segundos))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
    if fsize_bytes > 0:
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
    if memoria_bytes > 0:
        try:
            resource.setrlimit(resource.RLIMIT_AS, (memoria_bytes, memoria_bytes))
        except (ValueError, OSError):
            pass  # some OSes (macOS) restrict RLIMIT_AS; the Linux container enforces it


def executar_python(codigo: str, entradas: dict[str, str], parametros: dict, saida: Path) -> None:
    """Run `codigo` in the sandbox: inputs (name -> parquet) become VIEWs; the script defines
    `resultado`; the subprocess writes `saida` (Parquet). Raises ErroAplicacao on failure,
    timeout or limit."""
    s = get_settings()
    with tempfile.TemporaryDirectory(prefix="cflow_py_") as td:
        d = Path(td)
        (d / "codigo.py").write_text(codigo, encoding="utf-8")
        (d / "runner.py").write_text(_RUNNER, encoding="utf-8")
        mani = {
            "codigo_path": str(d / "codigo.py"),
            "entradas": entradas,
            "parametros": parametros,
            "saida": str(saida),
            "memoria_duckdb": (
                f"{max(256, s.sandbox_memoria_mb)}MB" if s.sandbox_memoria_mb else "2GB"
            ),
        }
        (d / "manifesto.json").write_text(json.dumps(mani, default=str), encoding="utf-8")
        mem_bytes = int(s.sandbox_memoria_mb) * 1024 * 1024
        fsize_bytes = int(s.sandbox_saida_max_mb) * 1024 * 1024
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(d / "runner.py"), str(d / "manifesto.json")],
                cwd=td,
                env={"PATH": "/usr/bin:/bin", "HOME": td, "TMPDIR": td},
                capture_output=True,
                text=True,
                timeout=int(s.sandbox_timeout_segundos),
                preexec_fn=lambda: _limites(mem_bytes, fsize_bytes),
            )
        except subprocess.TimeoutExpired as exc:
            raise ErroAplicacao(
                codigo="sandbox_timeout",
                mensagem=f"Python node exceeded the time limit ({s.sandbox_timeout_segundos}s).",
                status=400,
            ) from exc
        if proc.returncode != 0:
            # the REAL detail goes to the node log (captured by the engine); the envelope stays generic.
            stderr = (proc.stderr or "").strip()[-2000:]
            raise ErroAplicacao(
                codigo="sandbox_falha",
                mensagem="Failed to run the python node." + (f" {stderr}" if stderr else ""),
                status=400,
            )
        if not saida.exists():
            raise ErroAplicacao(
                codigo="sandbox_sem_saida",
                mensagem="The python node produced no output (define `resultado`).",
                status=400,
            )
