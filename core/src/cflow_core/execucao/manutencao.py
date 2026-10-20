"""Snapshot maintenance: purge by age, preserving the LAST run of each pipeline (so cube queries
keep working). The audit trail (execucao_nos) stays; only the data Parquet is removed."""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

from sqlalchemy import text

from ..config import get_settings
from ..db import transacao


async def expurgar_snapshots(*, manter_dias: int = 30) -> dict:
    """Remove snapshot directories of runs finished more than `manter_dias` ago, except the latest
    run of each pipeline. Returns {removidos, mantidos}."""
    async with transacao() as conn:
        manter = set(
            (
                await conn.execute(
                    text(
                        "SELECT DISTINCT ON (pipeline_id) id FROM execucoes "
                        "WHERE status = 'concluida' ORDER BY pipeline_id, concluida_em DESC"
                    )
                )
            )
            .scalars()
            .all()
        )
        antigas = (
            (
                await conn.execute(
                    text(
                        "SELECT id FROM execucoes WHERE concluida_em IS NOT NULL "
                        "AND concluida_em < now() - make_interval(days => :d)"
                    ),
                    {"d": manter_dias},
                )
            )
            .scalars()
            .all()
        )

    base = Path(get_settings().snapshot_dir)
    removidos = 0
    for eid in antigas:
        if eid in manter:
            continue
        alvo = base / str(eid)
        if alvo.exists():
            shutil.rmtree(alvo, ignore_errors=True)
            removidos += 1
    return {"removidos": removidos, "mantidos": len(manter)}


# Above this fraction of directories deemed orphans, the routine REFUSES to delete without `forcar`.
# The signal it reads ("this directory is not in the table") only holds if the table belongs to the
# SAME environment as the directory. Pointed at the wrong database (test database, partial restore,
# freshly created base) it classifies EVERYTHING as orphan and wipes the whole disk.
LIMITE_ORFAOS = 0.95


async def expurgar_snapshots_orfaos(*, idade_minima_dias: int = 1, forcar: bool = False) -> dict:
    """Remove snapshot directories whose run NO LONGER EXISTS in the `execucoes` table.

    Why they exist: deleting a pipeline runs `DELETE FROM execucoes WHERE pipeline_id = ...`
    (`pipelines/service.py`) and the row disappears, but the Parquet stays. The age-based purge
    never reaches it: it scans the table, and what was deleted from it is invisible to the scan.
    The leftovers grow with every deleted pipeline.

    Three guards, because this is the only routine that deletes files by inference:

    * **Database with no runs at all: delete nothing.** With no runs to compare, "orphan" loses its
      meaning, and the right answer is to do nothing, not everything.
    * **More than `LIMITE_ORFAOS` looking like orphans: refuse** (unless `forcar=True`). A
      legitimate cleanup removes a part; removing almost everything is a sign of the wrong database.
    * **`idade_minima_dias`** protects against races: a run that just created the directory and
      has not written its row yet would look orphaned.

    Only deletes directories whose name is a valid UUID; anything else was not created by this
    platform, and wrongly deleting a neighbouring directory is worse than leaving garbage.
    """
    base = Path(get_settings().snapshot_dir)
    if not base.exists():
        return {"removidos": 0, "bytes": 0, "ignorados": 0}

    async with transacao() as conn:
        vivas = {
            str(i) for i in (await conn.execute(text("SELECT id FROM execucoes"))).scalars().all()
        }

    limite_idade = time.time() - idade_minima_dias * 86400
    candidatos: list[Path] = []
    diretorios_uuid, ignorados = 0, 0
    for alvo in base.iterdir():
        if not alvo.is_dir():
            continue
        try:
            uuid.UUID(alvo.name)
        except ValueError:
            ignorados += 1
            continue
        diretorios_uuid += 1
        if alvo.name in vivas or alvo.stat().st_mtime >= limite_idade:
            continue
        candidatos.append(alvo)

    if not vivas:
        return {
            "removidos": 0,
            "bytes": 0,
            "ignorados": ignorados,
            "recusado": "database has no runs, nothing to compare (wrong database?)",
            "candidatos": len(candidatos),
        }
    proporcao = len(candidatos) / diretorios_uuid if diretorios_uuid else 0.0
    if proporcao > LIMITE_ORFAOS and not forcar:
        return {
            "removidos": 0,
            "bytes": 0,
            "ignorados": ignorados,
            "recusado": (
                f"{len(candidatos)} of {diretorios_uuid} directories ({proporcao:.0%}) "
                "looked orphaned, above the safety limit. Check that the database belongs to "
                "the same environment; use forcar=True if that is really the case."
            ),
            "candidatos": len(candidatos),
        }

    removidos, bytes_ = 0, 0
    for alvo in candidatos:
        bytes_ += sum(f.stat().st_size for f in alvo.rglob("*") if f.is_file())
        shutil.rmtree(alvo, ignore_errors=True)
        removidos += 1
    return {"removidos": removidos, "bytes": bytes_, "ignorados": ignorados}
