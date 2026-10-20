"""Access to ingestion metadata (sources, runs, batches, watermarks, quarantine).

SQL lives in one place: the engine and the API talk through these functions. Transactions are
SHORT on purpose (same lesson as `conexoes.obter_acesso`): a transaction held open during a large
download pins the whole database's `xmin` and autovacuum stops reclaiming dead tuples in EVERY table.
"""

from __future__ import annotations

import json
import uuid
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..db import transacao
from ..errors import ErroAplicacao
from ..timezone import FUSO_SAO_PAULO, agora_utc


def _json(d) -> str:
    return json.dumps(d, ensure_ascii=False, default=str)


# --- Sources -------------------------------------------------------------------------------


async def carregar_fonte(conn: AsyncConnection, fonte_id: uuid.UUID) -> dict:
    r = (
        (
            await conn.execute(
                text(
                    "SELECT id, nome, dataset, classe, contrato, destino_conexao_id, ativo, sistema, "
                    "cadencia, proxima_em, criado_por FROM ingestao_fontes WHERE id = :id"
                ),
                {"id": fonte_id},
            )
        )
        .mappings()
        .first()
    )
    if r is None:
        raise ErroAplicacao(
            codigo="nao_encontrado", mensagem="External source not found.", status=404
        )
    return dict(r)


async def obter_fonte(fonte_id: uuid.UUID | str) -> dict:
    """Loads the source in its own short transaction (used by the engine/worker)."""
    async with transacao() as conn:
        return await carregar_fonte(conn, uuid.UUID(str(fonte_id)))


async def listar_fontes(conn: AsyncConnection) -> list[dict]:
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT f.id, f.nome, f.dataset, f.classe, f.ativo, f.sistema, f.cadencia, "
                    "f.proxima_em, "
                    "f.destino_conexao_id, f.contrato, w.chave_lote AS watermark, w.momento AS "
                    "watermark_em, "
                    "(SELECT max(e.iniciada_em) FROM ingestao_execucoes e WHERE e.fonte_id = f.id) "
                    "AS ultima_execucao_em, "
                    "(SELECT e.status FROM ingestao_execucoes e WHERE e.fonte_id = f.id "
                    " ORDER BY e.iniciada_em DESC LIMIT 1) AS ultimo_status "
                    "FROM ingestao_fontes f LEFT JOIN ingestao_watermarks w ON w.fonte_id = f.id "
                    "ORDER BY f.nome"
                )
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


# --- Runs ----------------------------------------------------------------------------


async def criar_execucao(
    conn: AsyncConnection,
    *,
    fonte_id: uuid.UUID,
    origem: str,
    janela_inicio: date | None,
    janela_fim: date | None,
    criado_por: uuid.UUID | None,
) -> uuid.UUID:
    return (
        await conn.execute(
            text(
                "INSERT INTO ingestao_execucoes (fonte_id, origem, janela_inicio, janela_fim, "
                "criado_por) VALUES (:f, :o, :i, :fim, :por) RETURNING id"
            ),
            {"f": fonte_id, "o": origem, "i": janela_inicio, "fim": janela_fim, "por": criado_por},
        )
    ).scalar_one()


async def finalizar_execucao(
    execucao_id: uuid.UUID | str, *, contagens: dict, erro: str | None = None
) -> None:
    """Closes the run with the SIX outcomes that `totais()` computes.

    The last two (`nao_publicados`, `nao_modificados`) used to be dropped here: a 31-day window that
    brought 22 batches and found 9 weekends was recorded as "22 batches", with nothing to tell a
    non-event from a lost slice. Whoever opens the screen to understand the difference is exactly
    the one suspecting a gap in the data."""
    async with transacao() as conn:
        await conn.execute(
            text(
                "UPDATE ingestao_execucoes SET status = :s, erro = :e, concluida_em = now(), "
                "lotes = :l, lotes_novos = :ln, lotes_reusados = :lr, lotes_quarentena = :lq, "
                "lotes_nao_publicados = :lnp, lotes_nao_modificados = :lnm "
                "WHERE id = :id"
            ),
            {
                "s": "falha" if erro else "concluida",
                "e": erro,
                "l": contagens.get("lotes", 0),
                "ln": contagens.get("novos", 0),
                "lr": contagens.get("reusados", 0),
                "lq": contagens.get("quarentena", 0),
                "lnp": contagens.get("nao_publicados", 0),
                "lnm": contagens.get("nao_modificados", 0),
                "id": uuid.UUID(str(execucao_id)),
            },
        )


async def consultar_execucao(conn: AsyncConnection, execucao_id: uuid.UUID) -> dict:
    """Run + its batches + its quarantine.

    **Beware the `lotes` key:** the column `ingestao_execucoes.lotes` is the COUNT (int), and the
    returned dict overwrites it with the LIST of batches. To get the number, use `lotes_novos`,
    `lotes_reusados` etc., or the totals `service.processar` returns; reading `["lotes"]` expecting
    an int yields `date`/`datetime` objects the audit JSON cannot serialize."""
    r = (
        (
            await conn.execute(
                text(
                    "SELECT e.*, f.nome AS fonte_nome, f.dataset FROM ingestao_execucoes e "
                    "JOIN ingestao_fontes f ON f.id = e.fonte_id WHERE e.id = :id"
                ),
                {"id": execucao_id},
            )
        )
        .mappings()
        .first()
    )
    if r is None:
        raise ErroAplicacao(
            codigo="nao_encontrado", mensagem="Ingestion run not found.", status=404
        )
    lotes = (
        (
            await conn.execute(
                text(
                    "SELECT chave_lote, sha256, bytes, linhas, status, landing_uri, bronze_uri, "
                    "ingestion_date, criado_em FROM ingestao_lotes WHERE execucao_id = :id "
                    "ORDER BY chave_lote"
                ),
                {"id": execucao_id},
            )
        )
        .mappings()
        .all()
    )
    quarentena = (
        (
            await conn.execute(
                text(
                    "SELECT chave_lote, etapa, codigo, mensagem, uri, criado_em "
                    "FROM ingestao_quarentena WHERE execucao_id = :id ORDER BY criado_em"
                ),
                {"id": execucao_id},
            )
        )
        .mappings()
        .all()
    )
    return {
        **dict(r),
        "lotes": [dict(x) for x in lotes],
        "quarentena": [dict(x) for x in quarentena],
    }


async def listar_execucoes(
    conn: AsyncConnection, *, fonte_id: uuid.UUID | None, limite: int
) -> list[dict]:
    sql = (
        "SELECT e.id, e.fonte_id, f.nome AS fonte_nome, f.dataset, e.status, e.origem, "
        "e.janela_inicio, e.janela_fim, e.lotes, e.lotes_novos, e.lotes_reusados, "
        "e.lotes_quarentena, e.lotes_nao_publicados, e.lotes_nao_modificados, "
        "e.erro, e.iniciada_em, e.concluida_em, "
        # Slices seen vs. slices needing a retry: the pair the list needs to say "31 slices,
        # 2 to retry" without a second call per history row.
        "(SELECT count(*) FROM ingestao_fatias x WHERE x.execucao_id = e.id) AS fatias, "
        "(SELECT count(*) FROM ingestao_fatias x WHERE x.execucao_id = e.id "
        " AND x.desfecho IN ('falha','quarentena')) AS fatias_com_falha "
        "FROM ingestao_execucoes e JOIN ingestao_fontes f ON f.id = e.fonte_id"
    )
    params: dict = {"lim": limite}
    if fonte_id is not None:
        sql += " WHERE e.fonte_id = :f"
        params["f"] = fonte_id
    sql += " ORDER BY e.iniciada_em DESC LIMIT :lim"
    return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]


# --- Per-slice log ---------------------------------------------------------------------------

# Priority of a slice's SUMMARIZED outcome. A slice may yield several requests (a multi-file zip,
# paginated OData), and the summary must show what needs attention: a slice that wrote 8 files and
# quarantined 1 is "quarentena", not "novo".
PRIORIDADE = (
    "falha",
    "quarentena",
    "novo",
    "reusado",
    "nao_modificado",
    "nao_publicado",
)


def desfecho_de(contagens: dict) -> str:
    """Summarizes a slice's counts into a single outcome. `vazio` (empty) = the connector returned
    no request at all (e.g. a contract that shrinks the window); a legitimate state, and a silent
    one if it had no name."""
    for d in PRIORIDADE:
        if contagens.get(d):
            return d
    return "vazio"


async def registrar_fatia(
    *,
    execucao_id: uuid.UUID | str,
    fonte_id: uuid.UUID | str,
    chave_lote: str,
    janela_inicio: date | None,
    janela_fim: date | None,
    contagens: dict,
    duracao_ms: int,
    erro: str | None = None,
    desfecho: str | None = None,
) -> None:
    """Records the outcome of ONE slice. Upsert by `(execucao_id, chave_lote)` with `tentativa`
    incremented: the Temporal activity retries the same slice up to 3 times, and without the unique
    key a slice that failed twice and passed on the third try would become three rows; the screen
    would count 3 slices where there is 1, with two "failures" the platform already resolved by
    itself.

    Its OWN short transaction, like the rest of this module: the slice is recorded right after a
    download that may have taken minutes, and holding a transaction during it stalls autovacuum for
    the whole database."""
    async with transacao() as conn:
        await conn.execute(
            text(
                "INSERT INTO ingestao_fatias (execucao_id, fonte_id, chave_lote, janela_inicio, "
                "janela_fim, desfecho, contagens, duracao_ms, erro) "
                "VALUES (:e, :f, :c, :ji, :jf, :d, CAST(:ct AS jsonb), :dur, :err) "
                "ON CONFLICT ON CONSTRAINT uq_ingestao_fatia DO UPDATE SET "
                "desfecho = EXCLUDED.desfecho, contagens = EXCLUDED.contagens, "
                "duracao_ms = EXCLUDED.duracao_ms, erro = EXCLUDED.erro, "
                "concluida_em = now(), tentativa = ingestao_fatias.tentativa + 1"
            ),
            {
                "e": uuid.UUID(str(execucao_id)),
                "f": uuid.UUID(str(fonte_id)),
                "c": chave_lote,
                "ji": janela_inicio,
                "jf": janela_fim,
                "d": desfecho or desfecho_de(contagens),
                "ct": _json(contagens or {}),
                "dur": duracao_ms,
                "err": (erro or None) and erro[:2000],
            },
        )


async def listar_fatias(conn: AsyncConnection, execucao_id: uuid.UUID) -> list[dict]:
    """The run's LOG: one row per slice, with the reason attached.

    The quarantine reason lives in `ingestao_quarentena` (stage/code/message) and is what the reader
    came for; joining it here spares the screen from cross-referencing two lists by `chave_lote` and
    getting it wrong when the same slice failed at two stages. `LATERAL ... LIMIT 1` brings the most
    recent one."""
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT x.chave_lote, x.janela_inicio, x.janela_fim, x.desfecho, x.contagens, "
                    "x.tentativa, x.duracao_ms, x.erro, x.iniciada_em, x.concluida_em, "
                    "q.etapa AS quarentena_etapa, q.codigo AS quarentena_codigo, "
                    "q.mensagem AS quarentena_mensagem, q.uri AS quarentena_uri, "
                    "l.linhas, l.bytes, l.bronze_uri "
                    "FROM ingestao_fatias x "
                    "LEFT JOIN LATERAL (SELECT etapa, codigo, mensagem, uri FROM "
                    " ingestao_quarentena q2 WHERE q2.execucao_id = x.execucao_id "
                    " AND q2.chave_lote = x.chave_lote ORDER BY q2.criado_em DESC LIMIT 1) q "
                    " ON true "
                    "LEFT JOIN LATERAL (SELECT linhas, bytes, bronze_uri FROM ingestao_lotes l2 "
                    " WHERE l2.fonte_id = x.fonte_id AND l2.chave_lote = x.chave_lote "
                    " AND l2.status = 'bronze' ORDER BY l2.criado_em DESC LIMIT 1) l ON true "
                    "WHERE x.execucao_id = :id ORDER BY x.chave_lote"
                ),
                {"id": execucao_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def fatias_retentaveis(conn: AsyncConnection, execucao_id: uuid.UUID) -> list[dict]:
    """Slices of this run that ended badly: the CLOSED set a retry accepts.

    This query is what stops a retry from becoming a backfill through the back door: the caller does
    not choose the window, only among what ALREADY failed here. The network cost stays bounded by
    the original run's, which is what lets retry be a specialist action while backfill is an admin
    one."""
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT chave_lote, janela_inicio, janela_fim, desfecho, tentativa, erro "
                    "FROM ingestao_fatias WHERE execucao_id = :id "
                    "AND desfecho IN ('falha','quarentena') ORDER BY chave_lote"
                ),
                {"id": execucao_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def historico_da_fatia(
    conn: AsyncConnection, *, fonte_id: uuid.UUID, chave_lote: str, limite: int
) -> list[dict]:
    """Every time THIS slice ran, in any run. Answers "since when has the 2026-08-03 slice been
    quarantined?", which a single run's log can only answer for that run."""
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT x.execucao_id, e.origem, e.status AS execucao_status, x.desfecho, "
                    "x.tentativa, x.duracao_ms, x.erro, x.concluida_em "
                    "FROM ingestao_fatias x JOIN ingestao_execucoes e ON e.id = x.execucao_id "
                    "WHERE x.fonte_id = :f AND x.chave_lote = :c "
                    # TOTAL order, not partial. `concluida_em DESC` alone leaves two passes that
                    # finished at the same instant in undefined order, and Postgres does not
                    # promise stability between runs of the same query. For someone reading "since
                    # when has this slice been like this?", a list that reorders between two
                    # refreshes is worse than a wrong one: it destroys trust with no symptom.
                    #
                    # `NULLS FIRST` is EXPLICIT because it is what we want here, not merely
                    # Postgres's default for DESC: a slice still running is the most recent pass,
                    # and putting it last would show the old outcome as if it were current.
                    #
                    # Tie-break by `iniciada_em` (the pass that STARTED later is the most recent)
                    # and finally `id`, which is not chronological but guarantees a total order
                    # when both instants coincide.
                    "ORDER BY x.concluida_em DESC NULLS FIRST, x.iniciada_em DESC, x.id DESC "
                    "LIMIT :lim"
                ),
                {"f": fonte_id, "c": chave_lote, "lim": limite},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


# --- Batches --------------------------------------------------------------------------------


async def ultimo_lote_da_fatia(fonte_id: uuid.UUID, chave_lote: str) -> dict | None:
    """Latest batch of THIS slice; it supplies the ETag/Last-Modified for the conditional GET.

    Per slice, not per source: the ETag of July's file says nothing about August's."""
    async with transacao() as conn:
        r = (
            (
                await conn.execute(
                    text(
                        "SELECT id, sha256, etag, ultima_modificacao, status, landing_uri, "
                        "ingestion_date FROM ingestao_lotes WHERE fonte_id = :f AND chave_lote = :c "
                        "ORDER BY criado_em DESC LIMIT 1"
                    ),
                    {"f": fonte_id, "c": chave_lote},
                )
            )
            .mappings()
            .first()
        )
        return dict(r) if r else None


async def lote_por_hash(fonte_id: uuid.UUID, chave_lote: str, sha256: str) -> dict | None:
    async with transacao() as conn:
        r = (
            (
                await conn.execute(
                    text(
                        "SELECT id, status, landing_uri, bronze_uri, linhas, ingestion_date "
                        "FROM ingestao_lotes WHERE fonte_id = :f AND chave_lote = :c AND sha256 = :s"
                    ),
                    {"f": fonte_id, "c": chave_lote, "s": sha256},
                )
            )
            .mappings()
            .first()
        )
        return dict(r) if r else None


async def registrar_lote(
    *,
    fonte_id: uuid.UUID,
    execucao_id: uuid.UUID | None,
    chave_lote: str,
    sha256: str,
    bytes_: int,
    etag: str | None,
    ultima_modificacao: str | None,
    origem_url: str,
    landing_uri: str,
    ingestion_date: date,
    manifesto: dict,
) -> uuid.UUID | None:
    """Inserts the batch. Returns None if it ALREADY EXISTS (source, slice, hash); the race is
    resolved by the unique index, not by a SELECT before the INSERT: two workers on the same slice
    would both pass the SELECT and write twice."""
    async with transacao() as conn:
        r = (
            await conn.execute(
                text(
                    "INSERT INTO ingestao_lotes (fonte_id, execucao_id, chave_lote, sha256, bytes, "
                    "etag, ultima_modificacao, origem_url, landing_uri, ingestion_date, manifesto, "
                    "status) VALUES (:f, :e, :c, :s, :b, :et, :um, :ou, :lu, :id, "
                    "CAST(:m AS jsonb), "
                    "'landing') ON CONFLICT ON CONSTRAINT uq_ingestao_lote DO NOTHING RETURNING id"
                ),
                {
                    "f": fonte_id,
                    "e": execucao_id,
                    "c": chave_lote,
                    "s": sha256,
                    "b": bytes_,
                    "et": etag,
                    "um": ultima_modificacao,
                    "ou": origem_url,
                    "lu": landing_uri,
                    "id": ingestion_date,
                    "m": _json(manifesto),
                },
            )
        ).scalar_one_or_none()
        return r


async def concluir_lote(
    lote_id: uuid.UUID,
    *,
    bronze_uri: str,
    linhas: int,
    manifesto: dict,
    landing_uri: str | None = None,
) -> None:
    """Closes the batch in bronze. `landing_uri` is rewritten when the DESTINATION changed (lake
    migration): the batch is still the same (same bytes, same hash) but now lives elsewhere, and
    pointing at the old address would break replay."""
    async with transacao() as conn:
        await conn.execute(
            text(
                "UPDATE ingestao_lotes SET status = 'bronze', bronze_uri = :b, linhas = :l, "
                "manifesto = CAST(:m AS jsonb), "
                "landing_uri = COALESCE(:lu, landing_uri) WHERE id = :id"
            ),
            {"b": bronze_uri, "l": linhas, "m": _json(manifesto), "lu": landing_uri, "id": lote_id},
        )


async def marcar_lote_quarentena(lote_id: uuid.UUID) -> None:
    async with transacao() as conn:
        await conn.execute(
            text("UPDATE ingestao_lotes SET status = 'quarentena' WHERE id = :id"), {"id": lote_id}
        )


async def lotes_da_fonte(
    fonte_id: uuid.UUID, *, chaves: list[str] | None = None, so_com_landing: bool = True
) -> list[dict]:
    """Batches already landed: the raw material for REPLAY (reprocessing without touching the network)."""
    sql = (
        "SELECT DISTINCT ON (chave_lote) id, chave_lote, sha256, landing_uri, bronze_uri, linhas, "
        "ingestion_date, manifesto, status, origem_url, etag, ultima_modificacao, bytes "
        "FROM ingestao_lotes WHERE fonte_id = :f"
    )
    params: dict = {"f": fonte_id}
    if so_com_landing:
        sql += " AND landing_uri IS NOT NULL"
    if chaves is not None:
        sql += " AND chave_lote = ANY(:c)"
        params["c"] = chaves
    sql += " ORDER BY chave_lote, criado_em DESC"
    async with transacao() as conn:
        return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]


async def lotes_bronze(
    fonte_id: uuid.UUID | str,
    *,
    chaves: list[str] | None = None,
    chave_min: str | None = None,
    chave_max: str | None = None,
) -> list[dict]:
    """Latest batch IN BRONZE for each slice: the lake's **read catalog**.

    It is the lake-side snapshot resolver, and it is what resolves duplication by republication:
    annual files that are resubmitted every period make the same `chave_lote` accumulate versions.
    `DISTINCT ON (chave_lote) ... criado_em DESC` yields one per slice, and no pipeline has to
    remember that.

    `status='bronze'` goes in the WHERE BEFORE the `DISTINCT ON`, and the order matters: if a
    slice's most recent reload landed in quarantine, the filter lets the last GOOD batch show up
    instead of hiding the whole slice behind the failure.

    `chave_min`/`chave_max` compare lexicographically: keys are `YYYY`, `YYYY-MM`, `YYYY-MM-DD` or
    `a..b`, formats where alphabetical order is chronological order. This is the cut that separates
    an annual dataset (up to 2025) from a daily one (2026 onwards) when they overlap.
    """
    sql = (
        "SELECT DISTINCT ON (chave_lote) id, chave_lote, sha256, bronze_uri, landing_uri, linhas, "
        "bytes, ingestion_date, manifesto FROM ingestao_lotes "
        "WHERE fonte_id = :f AND status = 'bronze' AND bronze_uri IS NOT NULL"
    )
    params: dict = {"f": uuid.UUID(str(fonte_id))}
    if chaves is not None:
        sql += " AND chave_lote = ANY(:c)"
        params["c"] = chaves
    if chave_min:
        sql += " AND chave_lote >= :mn"
        params["mn"] = chave_min
    if chave_max:
        sql += " AND chave_lote <= :mx"
        params["mx"] = chave_max
    sql += " ORDER BY chave_lote, criado_em DESC"
    async with transacao() as conn:
        return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]


# --- Watermark ----------------------------------------------------------------------------


async def ler_watermark(fonte_id: uuid.UUID | str) -> dict | None:
    async with transacao() as conn:
        r = (
            (
                await conn.execute(
                    text(
                        "SELECT chave_lote, momento, detalhe FROM ingestao_watermarks "
                        "WHERE fonte_id = :f"
                    ),
                    {"f": uuid.UUID(str(fonte_id))},
                )
            )
            .mappings()
            .first()
        )
        return dict(r) if r else None


async def gravar_watermark(
    fonte_id: uuid.UUID | str, chave_lote: str, detalhe: dict | None = None
) -> None:
    """Advances the watermark. It only ADVANCES: a replay of an old window must not make the source
    "forget" what it already ingested and reprocess everything again tomorrow."""
    async with transacao() as conn:
        await conn.execute(
            text(
                "INSERT INTO ingestao_watermarks (fonte_id, chave_lote, detalhe) "
                "VALUES (:f, :c, CAST(:d AS jsonb)) "
                "ON CONFLICT (fonte_id) DO UPDATE SET chave_lote = EXCLUDED.chave_lote, "
                "momento = now(), detalhe = EXCLUDED.detalhe "
                "WHERE ingestao_watermarks.chave_lote < EXCLUDED.chave_lote"
            ),
            {"f": uuid.UUID(str(fonte_id)), "c": chave_lote, "d": _json(detalhe or {})},
        )


# --- Quarantine (DLQ) -----------------------------------------------------------------------


async def registrar_quarentena(
    *,
    fonte_id: uuid.UUID,
    execucao_id: uuid.UUID | None,
    chave_lote: str,
    etapa: str,
    codigo: str,
    mensagem: str,
    uri: str | None = None,
    sha256: str | None = None,
) -> uuid.UUID:
    async with transacao() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO ingestao_quarentena (fonte_id, execucao_id, chave_lote, etapa, "
                    "codigo, mensagem, uri, sha256) VALUES (:f, :e, :c, :et, :co, :m, :u, :s) "
                    "RETURNING id"
                ),
                {
                    "f": fonte_id,
                    "e": execucao_id,
                    "c": chave_lote,
                    "et": etapa,
                    "co": codigo,
                    "m": mensagem[:2000],
                    "u": uri,
                    "s": sha256,
                },
            )
        ).scalar_one()


async def listar_quarentena(
    conn: AsyncConnection, *, fonte_id: uuid.UUID | None, limite: int
) -> list[dict]:
    sql = (
        "SELECT q.id, q.fonte_id, f.nome AS fonte_nome, q.execucao_id, q.chave_lote, q.etapa, "
        "q.codigo, q.mensagem, q.uri, q.criado_em FROM ingestao_quarentena q "
        "JOIN ingestao_fontes f ON f.id = q.fonte_id"
    )
    params: dict = {"lim": limite}
    if fonte_id is not None:
        sql += " WHERE q.fonte_id = :f"
        params["f"] = fonte_id
    sql += " ORDER BY q.criado_em DESC LIMIT :lim"
    return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]


def fatia_no_futuro(chave_lote: str, hoje: date) -> bool:
    """Does the slice start after today? Then there is no data to be missing.

    Same discipline as `404 = not published`: a future month has not happened yet, and listing it
    as a gap drowns the warning that matters in non-events; a warning that always shouts is a
    warning nobody reads. Accepts the three key spellings contracts produce (`YYYY`, `YYYY-MM`,
    `YYYY-MM-DD`) and the range form `start..end`; a key that is not temporal allows no claim, and
    answers `False`."""
    bruto = chave_lote.split("..")[0]
    partes = bruto.split("-")
    try:
        if len(partes) == 1:
            return date(int(partes[0]), 1, 1) > hoje
        if len(partes) == 2:
            return date(int(partes[0]), int(partes[1]), 1) > hoje
        return date.fromisoformat(bruto) > hoje
    except ValueError:
        return False


# The question that separates "already-resolved quarantine" from "gap in the data": did the
# quarantined slice ever have a GOOD batch in bronze? If never, that slice's data exists nowhere,
# and silver comes out consistent with bronze yet missing the current year, an outcome no
# cross-layer comparison guard can see.
#
# No time ordering on purpose: a good batch BEFORE the quarantine also means the slice has data in
# bronze. What is being measured is a gap in the data, not a sequence of events.
_SQL_SEM_LOTE_BOM = (
    "SELECT DISTINCT ON (q.fonte_id, q.chave_lote) q.fonte_id, q.chave_lote, q.codigo, q.etapa, "
    "q.criado_em AS ultimo_em "
    "FROM ingestao_quarentena q "
    "WHERE NOT EXISTS ("
    "  SELECT 1 FROM ingestao_lotes l "
    "  WHERE l.fonte_id = q.fonte_id AND l.chave_lote = q.chave_lote "
    "    AND l.status = 'bronze' AND l.bronze_uri IS NOT NULL) "
    "{filtro}"
    "ORDER BY q.fonte_id, q.chave_lote, q.criado_em DESC"
)


async def fatias_sem_lote_bom(
    conn: AsyncConnection,
    *,
    fonte_id: uuid.UUID | None = None,
    chave_min: str | None = None,
    chave_max: str | None = None,
    hoje: date | None = None,
) -> list[dict]:
    """Quarantined slices that NEVER landed in bronze: the real gap.

    This is the one place that can tell "quarantine resolved later" from "data that is missing", and
    it feeds the API summary.

    `chave_min`/`chave_max` compare lexicographically: keys are `YYYY`, `YYYY-MM` or `YYYY-MM-DD`,
    so lexicographic order is chronological (same yardstick as `lotes_bronze`). The future-slice
    cut happens in Python, not SQL, because it depends on interpreting the key's spelling.
    """
    filtro = "AND q.fonte_id = :f " if fonte_id is not None else ""
    params: dict = {}
    if fonte_id is not None:
        params["f"] = fonte_id
    rows = (
        (await conn.execute(text(_SQL_SEM_LOTE_BOM.format(filtro=filtro)), params)).mappings().all()
    )
    hoje = hoje or agora_utc().astimezone(FUSO_SAO_PAULO).date()
    return [
        dict(r)
        for r in rows
        if (not chave_min or r["chave_lote"] >= chave_min)
        and (not chave_max or r["chave_lote"] <= chave_max)
        and not fatia_no_futuro(r["chave_lote"], hoje)
    ]


async def resumo_quarentena(
    conn: AsyncConnection, *, fonte_id: uuid.UUID | None = None
) -> list[dict]:
    """Quarantine aggregated PER SOURCE: the report that gives the DLQ an owner.

    `listar_quarentena` answers "what fell in"; nobody opens a list of 500 raw rows to discover that
    five sources have been quarantined for months. The summary answers the operational questions:
    how many, since when, with which code, and above all **`fatias_sem_lote_bom`**, the field that
    changes the decision: quarantine with a good batch in bronze is already-resolved noise;
    quarantine without one is data that does not exist.

    Two queries, not one per source: the aggregate and the list of slices without a good batch
    (which needs the future-slice cut in Python). Read-only: it writes nothing, so it records
    nothing in the audit trail.
    """
    filtro = "WHERE q.fonte_id = :f " if fonte_id is not None else ""
    params: dict = {}
    if fonte_id is not None:
        params["f"] = fonte_id
    sql = (
        "SELECT q.fonte_id, f.nome AS fonte_nome, f.dataset, f.ativo, "
        "count(*) AS total, "
        "count(*) FILTER (WHERE q.criado_em >= now() - interval '7 days') AS ultimos_7d, "
        "count(*) FILTER (WHERE q.criado_em >= now() - interval '30 days') AS ultimos_30d, "
        "min(q.criado_em) AS primeiro_em, max(q.criado_em) AS ultimo_em "
        "FROM ingestao_quarentena q JOIN ingestao_fontes f ON f.id = q.fonte_id "
        f"{filtro}"
        "GROUP BY q.fonte_id, f.nome, f.dataset, f.ativo "
        "ORDER BY count(*) DESC, f.nome"
    )
    linhas = [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]

    # Top-3 codes per source. A single `codigo` answers "what to fix"; the whole list in count
    # order answers "and what else", without becoming a table dump.
    sql_cod = (
        "SELECT fonte_id, codigo, contagem FROM ("
        "  SELECT q.fonte_id, q.codigo, count(*) AS contagem, "
        "         row_number() OVER (PARTITION BY q.fonte_id ORDER BY count(*) DESC, q.codigo) AS n"
        "  FROM ingestao_quarentena q "
        f"  {filtro}"
        "  GROUP BY q.fonte_id, q.codigo) t "
        "WHERE n <= 3 ORDER BY fonte_id, n"
    )
    codigos: dict[uuid.UUID, list[dict]] = {}
    for r in (await conn.execute(text(sql_cod), params)).mappings().all():
        codigos.setdefault(r["fonte_id"], []).append(
            {"codigo": r["codigo"], "contagem": r["contagem"]}
        )

    buracos: dict[uuid.UUID, int] = {}
    for r in await fatias_sem_lote_bom(conn, fonte_id=fonte_id):
        buracos[r["fonte_id"]] = buracos.get(r["fonte_id"], 0) + 1

    for ln in linhas:
        ln["codigos"] = codigos.get(ln["fonte_id"], [])
        ln["fatias_sem_lote_bom"] = buracos.get(ln["fonte_id"], 0)
    return linhas


async def fontes_cronicamente_quarentenadas(
    conn: AsyncConnection, *, minimo_execucoes: int = 3
) -> list[dict]:
    """Sources whose last `minimo_execucoes` runs were ALL quarantine, with no `novo`.

    A chronic-source signal: not the slice that failed yesterday (a retry fixes that), but the
    source that has brought no new data for several runs in a row and would keep doing so silently,
    because nobody reads the quarantine.

    Only FINISHED runs count (`concluida_em IS NOT NULL`): a run still in progress that has not yet
    reached the good slices would look like pure quarantine and invent a chronic case.

    The consumer (a scheduled alert) is a separate concern, and **should not be enabled** until the
    known chronic cases are resolved or declared: an alert that is born noisy is the "DLQ drowned
    in non-events" anti-pattern."""
    sql = (
        "WITH por_exec AS ("
        "  SELECT e.id, e.fonte_id, e.iniciada_em, "
        "         count(*) FILTER (WHERE x.desfecho = 'quarentena') AS quarentena, "
        "         count(*) FILTER (WHERE x.desfecho = 'novo') AS novos "
        "  FROM ingestao_execucoes e JOIN ingestao_fatias x ON x.execucao_id = e.id "
        "  WHERE e.concluida_em IS NOT NULL "
        "  GROUP BY e.id, e.fonte_id, e.iniciada_em), "
        "recentes AS ("
        "  SELECT *, row_number() OVER (PARTITION BY fonte_id ORDER BY iniciada_em DESC) AS pos "
        "  FROM por_exec) "
        "SELECT r.fonte_id, f.nome AS fonte_nome, f.dataset, f.ativo, "
        "       count(*) AS execucoes_consecutivas, sum(r.quarentena) AS fatias_quarentena, "
        "       min(r.iniciada_em) AS desde_em, max(r.iniciada_em) AS ultima_em "
        "FROM recentes r JOIN ingestao_fontes f ON f.id = r.fonte_id "
        "WHERE r.pos <= :n "
        "GROUP BY r.fonte_id, f.nome, f.dataset, f.ativo "
        "HAVING count(*) >= :n AND bool_and(r.quarentena > 0) AND bool_and(r.novos = 0) "
        "ORDER BY f.nome"
    )
    rows = (await conn.execute(text(sql), {"n": minimo_execucoes})).mappings().all()
    return [dict(r) for r in rows]


async def expurgar_quarentena_resolvida(
    conn: AsyncConnection, *, manter_dias: int = 180, simular: bool = False
) -> int:
    """Deletes OLD quarantine rows whose slice already has a good batch in bronze. Returns the count.

    **A row for a slice WITHOUT a good batch is never deleted, at any age.** Deleting the record of
    an unresolved gap deletes the evidence of the gap: quarantine would exist only for failures the
    platform already got past, and the question "why is this year missing from this dataset?" would
    have no answer anywhere in the product. It is the same predicate as `fatias_sem_lote_bom`,
    inverted (a single one, on purpose, so the two reads never disagree about what a gap is).

    What stays out: the raw object in the bucket's landing/quarantine area. Object expiry is a
    declarative lifecycle rule in infrastructure-as-code; implementing it here would make the
    application carry object DELETE permission on the bucket, which it does not have today and
    should not have."""
    onde = (
        "criado_em < now() - make_interval(days => CAST(:d AS int)) "
        "AND EXISTS ("
        "  SELECT 1 FROM ingestao_lotes l "
        "  WHERE l.fonte_id = ingestao_quarentena.fonte_id "
        "    AND l.chave_lote = ingestao_quarentena.chave_lote "
        "    AND l.status = 'bronze' AND l.bronze_uri IS NOT NULL)"
    )
    if simular:
        sql = f"SELECT count(*) FROM ingestao_quarentena WHERE {onde}"  # noqa: S608
        return int((await conn.execute(text(sql), {"d": manter_dias})).scalar_one())
    sql = f"DELETE FROM ingestao_quarentena WHERE {onde}"  # noqa: S608
    return int((await conn.execute(text(sql), {"d": manter_dias})).rowcount)
