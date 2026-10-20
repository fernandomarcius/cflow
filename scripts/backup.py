"""Backup and restore of the metadata database (Postgres), self-hosted.

Uses pg_dump/pg_restore from the Postgres container (docker-compose model). On a managed
deployment, run the same commands against the database host. The audit trail is permanent and IS
part of the backup. "A backup that was never restored is not a backup": `verificar_restauracao`
performs the round trip.

CLI:
  python scripts/backup.py dump <file>     # writes a dump (custom format)
  python scripts/backup.py verificar       # backup -> restore into a temp DB -> compare -> drop
"""

from __future__ import annotations

import os
import subprocess
import sys

CONTAINER = os.environ.get("CFLOW_PG_CONTAINER", "cflow-postgres-1")
DB = os.environ.get("POSTGRES_DB", "cflow")
USER = os.environ.get("POSTGRES_USER", "cflow_owner")
PW = os.environ.get("POSTGRES_PASSWORD", "")


def _docker(args: list[str], entrada: bytes | None = None) -> subprocess.CompletedProcess:
    base = ["docker", "exec", "-i", "-e", f"PGPASSWORD={PW}", CONTAINER]
    return subprocess.run(base + args, input=entrada, capture_output=True)


def fazer_backup(db: str = DB) -> bytes:
    """Return a custom-format (-Fc) dump of the database, audit trail included."""
    r = _docker(["pg_dump", "-Fc", "-U", USER, db])
    if r.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {r.stderr.decode()[:300]}")
    return r.stdout


def _sql_postgres(comando: str) -> subprocess.CompletedProcess:
    return _docker(["psql", "-v", "ON_ERROR_STOP=1", "-U", USER, "-d", "postgres", "-c", comando])


def restaurar(dump: bytes, destino: str) -> None:
    """Recreate the `destino` database from scratch and restore the dump into it."""
    _sql_postgres(f'DROP DATABASE IF EXISTS "{destino}"')
    r = _sql_postgres(f'CREATE DATABASE "{destino}"')
    if r.returncode != 0:
        raise RuntimeError(f"create db failed: {r.stderr.decode()[:300]}")
    rest = _docker(["pg_restore", "--no-owner", "-U", USER, "-d", destino], entrada=dump)
    # pg_restore exits non-zero on benign warnings (e.g. role already exists); a real failure
    # shows up as an error in stderr
    if (
        rest.returncode != 0
        and b"error" in rest.stderr.lower()
        and b"already exists" not in rest.stderr.lower()
    ):
        raise RuntimeError(f"pg_restore failed: {rest.stderr.decode()[:300]}")


def _contar(db: str, tabela: str) -> int:
    r = _docker(["psql", "-tA", "-U", USER, "-d", db, "-c", f"SELECT count(*) FROM {tabela}"])
    return int(r.stdout.decode().strip() or "0")


def verificar_restauracao(db_origem: str = DB, destino: str = "cflow_backup_check") -> dict:
    """Round trip: back up the source -> restore into the target -> compare counts -> drop target."""
    dump = fazer_backup(db_origem)
    restaurar(dump, destino)
    try:
        tabelas = ["auditoria", "usuarios", "conexoes", "pipelines"]
        resultado = {
            t: {"origem": _contar(db_origem, t), "restaurado": _contar(destino, t)} for t in tabelas
        }
        ok = all(v["origem"] == v["restaurado"] for v in resultado.values())
        return {"ok": ok, "tabelas": resultado, "tamanho_dump": len(dump)}
    finally:
        _sql_postgres(f'DROP DATABASE IF EXISTS "{destino}"')


def _reter(dir_backups: str, db: str, manter: int) -> None:
    import glob

    arquivos = sorted(glob.glob(os.path.join(dir_backups, f"{db}-*.dump")))
    for velho in arquivos[:-manter] if manter > 0 else []:
        os.remove(velho)


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "dump":
        import datetime

        dump = fazer_backup()
        if len(argv) > 2:
            destino = argv[2]
        else:
            carimbo = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005
            destino = os.path.join("backups", f"{DB}-{carimbo}.dump")
        os.makedirs(os.path.dirname(destino) or ".", exist_ok=True)
        with open(destino, "wb") as f:
            f.write(dump)
        # retention: keep the last N dumps of this database (CFLOW_BACKUP_KEEP, default 7)
        _reter(os.path.dirname(destino) or ".", DB, int(os.environ.get("CFLOW_BACKUP_KEEP", "7")))
        print(f"Backup of '{DB}' written to {destino} ({len(dump)} bytes).")
        return 0
    if len(argv) >= 2 and argv[1] == "verificar":
        r = verificar_restauracao()
        print(("OK" if r["ok"] else "FAILED"), "- backup->restore round trip:", r["tabelas"])
        return 0 if r["ok"] else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
