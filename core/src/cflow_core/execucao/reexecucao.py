"""Re-run an execution: the whole run, or from a node onwards (like Airflow's "clear task").

**Never reopens the previous execution.** It creates a NEW execution with `origem='reexecucao'`
and `reexecucao_de` pointing at the original. Rewriting yesterday's outcome would erase the
evidence that a failure happened, which is exactly what one came to investigate. Same principle as
the ingestion retry (`ingestao/service.py`), for the same reason.

**From a node X:** X's upstream does not run. Its snapshots are COPIED from the original execution
into the new one, and the `execucao_nos` rows enter as `reaproveitado=true`. It is the same move
as fingerprint reuse; the difference is that here the snapshot source is a CHOSEN execution, not
the most recent one with an equal fingerprint.

**Only X is forced, not its descendants.** X recomputes even with an identical fingerprint (that
is what "redo this node" means: whoever asks has seen the result and wants it discarded).
Descendants follow normal reuse: if X's content changes, their fingerprint changes and they
recompute on their own; if X comes out identical, reusing is the RIGHT answer, and forcing them
would burn time reproducing byte for byte what is already on disk.

**The external effect REALLY repeats.** The `efeito_disparado` lock is per `(execucao_id, no_id)`:
the new execution does not have it, so an `email`/`http`/`escrita_sql`-append node in the re-run
stretch sends/writes again. This is deliberately not blocked here: the decision belongs to the
operator, which is why `plano()` returns the NAMED effects so the UI can warn first.

**Publication inherits the decision of the ORIGINAL chain's ROOT.** The `sem_publicar` mark of a
chained trigger is not persisted: it is derived, on every trigger, from the root's definition
(`montante_publica: false`). Re-running the upstream of such a run without deriving it again would
publish with the as-of of the run that claims no publication. So the decision is taken again here,
from that chain's root, in the version it EXECUTED, and `plano()` names the stretch's outputs and
says whether they publish.
"""

from __future__ import annotations

import shutil
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import DetalheErro, ErroAplicacao
from ..pipelines import service as ps
from ..pipelines.definicao import montante_publica, nomes_segredo, ordem_topologica

# Node types that produce an effect OUTSIDE the platform. `idempotente` says whether repeating is
# harmless: `ingestao` is (the `(fonte, chave_lote, sha256)` uniqueness turns a repeated slice into
# a no-op); `http`/`email` never are; `escrita_sql` depends on the strategy (upsert/truncate_insert
# repeat without duplicating, append duplicates). The UI warning has to show this distinction:
# saying "there are effects" without saying which turns the alert into noise people learn to ignore.
TIPOS_EFEITO = ("escrita_sql", "http", "email", "ingestao")

# How many `reexecucao_de` links to climb to find the origin chain. A re-run is created OUTSIDE a
# chain (`cadeia_id` null), so re-running the re-run of an upstream only reaches the root by
# climbing this link. The cap is just a brake against corrupted data: nobody re-runs a hundred
# times in a row.
_MAX_ELOS_REEXECUCAO = 100


def _idempotente(no: dict) -> bool:
    tipo = no.get("tipo")
    if tipo == "ingestao":
        return True
    if tipo == "escrita_sql":
        cfg = no.get("config") or {}
        return cfg.get("modo") == "tabela" and cfg.get("estrategia") in (
            "upsert",
            "truncate_insert",
        )
    return False


def publicacao_prevista(no_id: str, no: dict, publica: bool) -> dict:
    """The stretch's output, NAMED: where it publishes. `alvo` is the dataset (lake/iceberg) or the
    file name (`export_dir`); `camada` only exists for `lake`, where it picks the bucket."""
    cfg = no.get("config") or {}
    destino = str(cfg.get("destino") or "arquivo").lower()
    return {
        "no_id": no_id,
        "destino": destino,
        "alvo": cfg.get("dataset") if destino in ("lake", "iceberg") else cfg.get("nome"),
        "camada": str(cfg.get("camada") or "silver") if destino == "lake" else None,
        "publica": publica,
    }


def descendentes(definicao: dict, no_id: str) -> set[str]:
    """`no_id` and everything that depends on it (transitive closure over the edges).

    It is the set that must run again: redoing X and leaving what consumes X with stale data would
    deliver an internally incoherent execution, worse than redoing nothing, because it looks like
    a complete execution."""
    adj: dict[str, list[str]] = {}
    for a in definicao.get("arestas", []):
        adj.setdefault(a["de"], []).append(a["para"])
    vistos = {no_id}
    fila = [no_id]
    while fila:
        atual = fila.pop()
        for viz in adj.get(atual, []):
            if viz not in vistos:
                vistos.add(viz)
                fila.append(viz)
    return vistos


async def _original(conn: AsyncConnection, execucao_id: uuid.UUID) -> dict:
    linha = (
        (
            await conn.execute(
                text(
                    "SELECT e.id, e.pipeline_id, e.versao_numero, e.status, e.parametros, "
                    "       e.cadeia_id, e.pagina_id, pl.nome AS pipeline_nome "
                    "FROM execucoes e JOIN pipelines pl ON pl.id = e.pipeline_id "
                    "WHERE e.id = :id"
                ),
                {"id": execucao_id},
            )
        )
        .mappings()
        .first()
    )
    if linha is None:
        raise ErroAplicacao(
            codigo="execucao_nao_encontrada", mensagem="Execution not found.", status=404
        )
    return dict(linha)


async def _motivo_sem_publicar(conn: AsyncConnection, execucao_id: uuid.UUID) -> str | None:
    """Why this re-run does NOT publish, or None when it publishes as usual.

    It does not publish when the execution is UPSTREAM of a chain whose root (`cadeia_raiz = true`)
    declares `montante_publica: false` in the version the root EXECUTED: the same decision as
    `disparo.decidir_montante`, taken from the same definition. The chain is the execution's own
    or, if it is already a re-run (created outside a chain), that of the closest ancestor through
    `reexecucao_de`: re-running the re-run of an upstream must not reopen what the first closed.
    Re-running the ROOT itself publishes: the mark is about the upstream.

    A chain without a marked root (older than the migration that added it) stays as it was: there
    is nobody to inherit from."""
    origem = (
        (
            await conn.execute(
                text(
                    "WITH RECURSIVE elo AS ("
                    "  SELECT id, cadeia_id, reexecucao_de, 0 AS n FROM execucoes WHERE id = :id "
                    "  UNION ALL "
                    "  SELECT e.id, e.cadeia_id, e.reexecucao_de, elo.n + 1 "
                    "  FROM execucoes e JOIN elo ON e.id = elo.reexecucao_de "
                    "  WHERE elo.cadeia_id IS NULL AND elo.n < :teto"
                    ") SELECT id, cadeia_id FROM elo WHERE cadeia_id IS NOT NULL "
                    "ORDER BY n LIMIT 1"
                ),
                {"id": execucao_id, "teto": _MAX_ELOS_REEXECUCAO},
            )
        )
        .mappings()
        .first()
    )
    if origem is None:
        return None
    raiz = (
        (
            await conn.execute(
                text(
                    "SELECT e.id, e.pipeline_id, e.versao_numero, pl.nome "
                    "FROM execucoes e JOIN pipelines pl ON pl.id = e.pipeline_id "
                    "WHERE e.cadeia_id = :c AND e.cadeia_raiz"
                ),
                {"c": origem["cadeia_id"]},
            )
        )
        .mappings()
        .first()
    )
    if raiz is None or raiz["id"] == origem["id"]:
        return None
    det = await ps.obter(conn, raiz["pipeline_id"], raiz["versao_numero"])
    if montante_publica(det["definicao"]):
        return None
    return (
        f"This execution is upstream of the '{raiz['nome']}' chain, whose root declares "
        '"montante_publica": false: the re-run recomputes and stores each node\'s result, but '
        "the outputs are not published, as in the original trigger."
    )


async def plano(conn: AsyncConnection, execucao_id: uuid.UUID, desde_no: str | None = None) -> dict:
    """What a re-run would do, WITHOUT doing it. It feeds the confirmation in the UI.

    Returns the nodes that would run, those that would be reused, the external effects the stretch
    contains (named, with real idempotency), the stretch's OUTPUTS (named: where they publish, and
    whether they do: `sem_publicar` says when not, and why) and the `segredo: true` parameters that
    must be provided again: the persisted value is `***` (masked), so it cannot be reused.

    Outputs are kept apart from effects because republishing duplicates nothing (it is another
    immutable version and a moved pointer), but it moves the `_atual.json` another consumer reads:
    whoever clicks needs to see WHICH pointer will change, and that does not fit the "does
    repeating duplicate?" of the effects list."""
    orig = await _original(conn, execucao_id)
    det = await ps.obter(conn, orig["pipeline_id"], orig["versao_numero"])
    definicao = det["definicao"]
    nos = {n["id"]: n for n in definicao.get("nos", [])}
    ordem = ordem_topologica(definicao)

    if desde_no is not None:
        if desde_no not in nos:
            # The definition is that of the EXECUTED VERSION. A node that existed in the execution
            # and no longer exists in it (edited graph, orphan node) cannot be redone, and refusing
            # here beats creating an execution that silently ignores the requested starting point.
            raise ErroAplicacao(
                codigo="no_inexistente",
                mensagem=f"Node '{desde_no}' does not exist in version {orig['versao_numero']} of this pipeline.",
                status=409,
            )
        alvo = descendentes(definicao, desde_no)
    else:
        alvo = set(nos)

    a_executar = [n for n in ordem if n in alvo]
    reaproveitados = [n for n in ordem if n not in alvo]
    efeitos = [
        {"no_id": n, "tipo": nos[n].get("tipo"), "idempotente": _idempotente(nos[n])}
        for n in a_executar
        if nos[n].get("tipo") in TIPOS_EFEITO
    ]
    motivo = await _motivo_sem_publicar(conn, execucao_id)
    publicacoes = [
        publicacao_prevista(n, nos[n], publica=motivo is None)
        for n in a_executar
        if nos[n].get("tipo") == "saida"
    ]
    return {
        "execucao_id": str(execucao_id),
        "pipeline_id": str(orig["pipeline_id"]),
        "pipeline_nome": orig["pipeline_nome"],
        "versao": orig["versao_numero"],
        "desde_no": desde_no,
        "nos_a_executar": a_executar,
        "nos_reaproveitados": reaproveitados,
        "efeitos": efeitos,
        "publicacoes": publicacoes,
        "sem_publicar": motivo is not None,
        "sem_publicar_motivo": motivo,
        # Names, never values: what is stored is `***`.
        "parametros_segredo": sorted(nomes_segredo(definicao)),
        "parametros": orig["parametros"] or {},
    }


async def _semear_montante(
    conn: AsyncConnection,
    *,
    origem_id: uuid.UUID,
    destino_id: uuid.UUID,
    nos: list[str],
    dir_destino,
) -> None:
    """Copy the snapshots of the nodes NOT re-run from the original execution into the new one.

    Without this the starting node would not find its input: `executar_no` reads the predecessor
    from `dir_exec/{pred}.parquet` (the execution's local file), not from the database. A snapshot
    that vanished from disk (retention purge) is an explicit refusal: running with a missing input
    would produce an empty result that looks like a result."""
    import os

    linhas = (
        (
            await conn.execute(
                text(
                    "SELECT no_id, status, linhas, hash, snapshot, fingerprint "
                    "FROM execucao_nos WHERE execucao_id = :e AND no_id = ANY(:ns)"
                ),
                {"e": origem_id, "ns": nos},
            )
        )
        .mappings()
        .all()
    )
    por_no = {r["no_id"]: r for r in linhas}
    for no_id in nos:
        r = por_no.get(no_id)
        # A node that never ran in the original (the execution failed before reaching it) is simply
        # not seeded: it is UPSTREAM of the starting point and, if some descendant needed it, the
        # request would be incoherent; but that was already decided by the `descendentes` cut,
        # which only leaves out what the target does not reach.
        if r is None or r["status"] != "concluida" or not r["snapshot"]:
            continue
        if not os.path.isfile(r["snapshot"]):
            raise ErroAplicacao(
                codigo="snapshot_ausente",
                mensagem=(
                    f"The result of node '{no_id}' is no longer on disk (snapshot retention). "
                    "Run the whole pipeline instead of re-running from a node."
                ),
                status=409,
            )
        destino = dir_destino / f"{no_id}.parquet"
        shutil.copy(r["snapshot"], destino)
        await conn.execute(
            text(
                "INSERT INTO execucao_nos (execucao_id, no_id, status, linhas, hash, snapshot, "
                "  duracao_ms, fingerprint, reaproveitado, concluido_em) "
                "VALUES (:e, :n, 'concluida', :l, :h, :s, 0, :fp, true, now()) "
                "ON CONFLICT (execucao_id, no_id) DO UPDATE SET "
                "  status='concluida', linhas=:l, hash=:h, snapshot=:s, duracao_ms=0, "
                "  fingerprint=:fp, reaproveitado=true, erro=NULL, concluido_em=now()"
            ),
            {
                "e": destino_id,
                "n": no_id,
                "l": r["linhas"],
                "h": r["hash"],
                "s": str(destino),
                "fp": r["fingerprint"],
            },
        )


async def preparar(
    *,
    execucao_id: uuid.UUID,
    ator_id: uuid.UUID,
    desde_no: str | None = None,
    parametros: dict | None = None,
    ip: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Create the new execution, seed the upstream and return the context ready to run.

    The caller (inline or Temporal) is the one that triggers, as in `criar_execucao`: both
    execution routes of the product already work this way. Returns the `criar_execucao` `ctx` plus
    a trimmed `ordem`, `forcar_no`, `fontes_execucao`, `sem_publicar` (inherited from the original
    chain's root, see `_motivo_sem_publicar`) and the plan used."""
    from ..db import transacao
    from .engine import _dir, criar_execucao

    async with transacao() as conn:
        p = await plano(conn, execucao_id, desde_no)
        orig = await _original(conn, execucao_id)

    # Secret: the persisted value is `***`, so reusing it would send the mask to the source. Either
    # whoever re-runs provides it again or the re-run is refused, never an execution that runs with
    # the wrong credential and fails further on, far from the cause.
    faltando = [n for n in p["parametros_segredo"] if not (parametros or {}).get(n)]
    if faltando:
        raise ErroAplicacao(
            codigo="parametro_segredo_obrigatorio",
            mensagem=(
                "Provide the credential parameter values again to re-run: "
                + ", ".join(faltando)
                + " (the value is not stored)."
            ),
            status=400,
            detalhes=[
                DetalheErro(campo=n, mensagem="Provide the value to re-run.") for n in faltando
            ],
        )

    # The original's parameters, with secrets restored by whoever asked for the re-run. Re-running
    # with OTHER parameters is not re-running, it is a new execution, and the Run button already
    # exists for that; so only the secret keys are accepted from the body.
    params = dict(p["parametros"])
    for n in p["parametros_segredo"]:
        params[n] = (parametros or {})[n]

    ctx = await criar_execucao(
        pipeline_id=orig["pipeline_id"],
        ator_id=ator_id,
        parametros=params,
        versao_numero=orig["versao_numero"],
        origem="reexecucao",
        reexecucao_de=execucao_id,
        reexecucao_desde_no=desde_no,
        ip=ip,
        request_id=request_id,
    )
    nova_id = uuid.UUID(ctx["execucao_id"])

    if p["nos_reaproveitados"]:
        dir_destino = _dir(nova_id)
        dir_destino.mkdir(parents=True, exist_ok=True)
        async with transacao() as conn:
            await _semear_montante(
                conn,
                origem_id=execucao_id,
                destino_id=nova_id,
                nos=p["nos_reaproveitados"],
                dir_destino=dir_destino,
            )

    # Freshness of `pipeline`-type sources: if the original belonged to a chain, this re-run reads
    # the EXACT snapshots of that run's siblings, not "the latest execution of any kind" of the
    # upstream, which would bring data from another scope/as_of. The map is the same one the
    # chained trigger builds at runtime, rebuilt from `cadeia_id`.
    fontes_execucao: dict[str, str] = {}
    if orig["cadeia_id"] is not None:
        async with transacao() as conn:
            irmas = (
                (
                    await conn.execute(
                        text(
                            "SELECT pipeline_id, id FROM execucoes "
                            "WHERE cadeia_id = :c AND status = 'concluida'"
                        ),
                        {"c": orig["cadeia_id"]},
                    )
                )
                .mappings()
                .all()
            )
        fontes_execucao = {str(r["pipeline_id"]): str(r["id"]) for r in irmas}

    return {
        **ctx,
        "ordem": p["nos_a_executar"],
        "forcar_no": desde_no,
        "fontes_execucao": fontes_execucao,
        # EXPLICIT signal down to `executar_no`/`finalizar` (inline) and `ExecutarPipeline`
        # (Temporal), never the mutated definition: the same contract as the chained trigger.
        "sem_publicar": p["sem_publicar"],
        "plano": p,
    }
