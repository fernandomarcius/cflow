"""Publishing silver as an **Apache Iceberg** table.

Why it exists: silver consumers are query engines such as Trino and BigQuery, and both speak
Iceberg natively. Loose Parquet is not a table for Trino - it would need a Hive Metastore - so the
catalog stops being an Iceberg-only cost. The tie-breaker is **schema evolution**: many silver
columns get typed later by whoever knows the business, and each such change, with loose Parquet,
means rewriting the dataset and resynchronising the metastore and external tables by hand.

Three choices underpin the rest:

- **JDBC catalog on the Postgres we already operate.** `SqlCatalog` creates `iceberg_tables` and
  `iceberg_namespace_properties` - the same schema the Trino connector reads with
  `iceberg.catalog.type=jdbc`. Writing and reading point at the SAME table, and there is no new
  service to run, monitor or restore.
- **`pg8000` driver (BSD), not `psycopg`.** PyIceberg uses synchronous SQLAlchemy and the project
  only has `asyncpg`. The obvious drivers (`psycopg2`, `psycopg`) are **LGPL**, which the licence
  check rejects. `pg8000` is pure Python and BSD.
- **Credentials never on disk.** PyIceberg accepts `gcs.oauth2.token`, so we reuse the access token
  the lake backend already obtains from the secret store. No `GOOGLE_APPLICATION_CREDENTIALS`, no
  loose service-account file, and PyArrow's built-in GCS client is enough.

What this module does NOT do: table maintenance (expiring snapshots, compacting small files,
removing orphans). That is a scheduled operational routine, not part of publishing.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from ..config import get_settings
from ..errors import ErroAplicacao
from . import tokenizacao
from .armazenamento import Armazenamento, ArmazenamentoGcs, ArmazenamentoLocal
from .contrato import Grao
from .contrato import grao as ler_grao

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pyarrow as pa

# Single silver namespace in the catalog. Trino sees it as a schema.
NAMESPACE = "silver"

# `dataset` becomes a table name in the catalog and a path in the bucket: keep the same slug the
# rest of the platform already validates, leaving no room for identifier injection.
_SLUG = re.compile(r"^[a-z0-9_]+$")


def _exigir_slug(dataset: str) -> str:
    if not _SLUG.match(dataset or ""):
        raise ErroAplicacao(
            codigo="iceberg_dataset_invalido",
            mensagem=(
                f"Invalid dataset name for Iceberg: '{dataset}'. "
                "Use only lowercase letters, digits and '_'."
            ),
            status=400,
        )
    return dataset


def _url_sincrona() -> str:
    """The catalog URL, using the synchronous driver PyIceberg requires.

    The platform uses `asyncpg` everywhere; PyIceberg's SQLAlchemy is synchronous and rejects it.
    `pg8000` is the translation - same URL, BSD driver."""
    url = get_settings().database_url
    if "+asyncpg" in url:
        return url.replace("+asyncpg", "+pg8000")
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+pg8000://", 1)
    return url


def _aplicar_tls(cat) -> None:
    """TLS-only Postgres: `SqlCatalog` creates the engine without `connect_args`, and pg8000 only
    accepts TLS through `ssl_context` - there is no URL parameter for it. The library engine is
    lazy (not connected yet), so it is swapped for one carrying the TLS context."""
    s = get_settings()
    if not s.database_tls:
        return
    from sqlalchemy import create_engine

    from ..runtime import contexto_ssl

    cat.engine.dispose()
    cat.engine = create_engine(
        _url_sincrona(),
        pool_pre_ping=True,
        connect_args={
            "ssl_context": contexto_ssl(s.database_ca_file),
            # Catalog tables live in the same schema as the application.
            **({"startup_params": {"search_path": s.database_schema}} if s.database_schema else {}),
        },
    )


async def _propriedades_do_lake(lake: Armazenamento) -> tuple[str, dict[str, str]]:
    """`(warehouse, properties)` of the backend - what lets the same code serve GCS and disk.

    On GCS the token comes from the secret store through the same path ingestion already uses; on
    local disk the warehouse is a `file://` URI, which keeps air-gapped mode working."""
    raiz = lake.raiz_uri("silver")
    if isinstance(lake, ArmazenamentoGcs):
        token, expira_em_ms = await lake.token_para_terceiros()
        return raiz, {
            "gcs.oauth2.token": token,
            "gcs.oauth2.token-expires-at": str(expira_em_ms),
            "py-io-impl": "pyiceberg.io.pyarrow.PyArrowFileIO",
        }
    if isinstance(lake, ArmazenamentoLocal):
        return raiz, {"py-io-impl": "pyiceberg.io.pyarrow.PyArrowFileIO"}
    raise ErroAplicacao(
        codigo="iceberg_backend_nao_suportado",
        mensagem="Iceberg publishing only supports the 'local' and 'gcs' lake backends.",
        status=400,
    )


async def abrir_catalogo(lake: Armazenamento):
    """JDBC catalog on Postgres, with the warehouse pointing at the lake silver layer."""
    try:
        from pyiceberg.catalog.sql import SqlCatalog
    except ImportError as e:  # pragma: no cover - dependency declared in pyproject
        raise ErroAplicacao(
            codigo="iceberg_indisponivel",
            mensagem="Iceberg support is not installed in this environment (pyiceberg).",
            status=500,
        ) from e

    warehouse, props = await _propriedades_do_lake(lake)
    # `init_catalog_tables=False`: the catalog tables come from a migration, because the runtime
    # connects as `cflow_app`, which has no DDL rights. Letting the library create them would break
    # the cflow_owner/cflow_app separation - and fail in production with "permission denied".
    # `"false"` as a STRING: PyIceberg properties go through `strtobool`, and a bool blows up with
    # `'bool' object has no attribute 'lower'`.
    # One `SqlCatalog` per (warehouse, properties) and per process. Each instance opens a
    # synchronous SQLAlchemy engine (pg8000) with its own pool against the SAME Postgres, and it was
    # never closed: with N activities publishing, N pools leaked. The properties are part of the key
    # because the GCS token expires and changes - new token, new catalog; the old one is dropped.
    chave = (warehouse, tuple(sorted(props.items())))
    cat = _CATALOGOS.get(chave)
    if cat is None:
        cat = SqlCatalog(
            "cflow", uri=_url_sincrona(), warehouse=warehouse, init_catalog_tables="false", **props
        )
        _aplicar_tls(cat)
        if not cat.namespace_exists(NAMESPACE):
            cat.create_namespace(NAMESPACE)
        for antiga in [k for k in _CATALOGOS if k[0] == warehouse]:
            _CATALOGOS.pop(antiga, None)
        _CATALOGOS[chave] = cat
    return cat


_CATALOGOS: dict[tuple, Any] = {}

# PyIceberg optimistic commit: the JDBC catalog does `UPDATE ... WHERE metadata_location = <the one I
# read>`, so two runs publishing the same table at the same time (a fan-out over the same
# silver/gold) make the second one fail with `CommitFailedException` - which used to surface raw,
# without an error code and without retrying. Reloading the table and redoing is what the exception
# asks for.
TENTATIVAS_COMMIT = 3
ESPERA_COMMIT_S = (0.5, 1.0, 2.0)


async def _commitar_com_retry(fazer, recarregar, *, dataset: str) -> None:
    """`fazer(tabela)` commits; `recarregar()` returns the table with current metadata. When the
    attempts run out, `409 iceberg_commit_conflito` - named, so the UI and audit trail say what
    happened."""
    from pyiceberg.exceptions import CommitFailedException

    tabela = recarregar()
    for i in range(TENTATIVAS_COMMIT):
        try:
            fazer(tabela)
            return
        except CommitFailedException as e:
            if i == TENTATIVAS_COMMIT - 1:
                raise ErroAplicacao(
                    codigo="iceberg_commit_conflito",
                    mensagem=(
                        f"Another run published table '{dataset}' at the same time; try again."
                    ),
                    status=409,
                ) from e
            await asyncio.sleep(ESPERA_COMMIT_S[i])
            tabela = recarregar()


def _schema_mudou(tabela, esquema: pa.Schema) -> bool:
    atual = tabela.schema().as_arrow()
    return [(c.name, str(c.type)) for c in atual] != [(c.name, str(c.type)) for c in esquema]


# --- Grain guard --------------------------------------------------------------------------------
# Why it lives HERE, and not only in a checker: publishing is the last moment at which the damage
# can still be avoided. After it, the wrong number is already in a table that query engines serve,
# and undoing it means publishing again - a consumer in between read duplicates.
#
# It is not delegated to PyIceberg: its `has_duplicate_rows` only runs on the `upsert` path (which
# this module does not use) and raises a raw `ValueError` - no error code, no readable message and
# no count of colliding rows, which is the number that decides what to do next.


def _mil(n: int) -> str:
    """Thousands separator. The message is read by people, and `2374` disappears in the middle of
    text where `2,374` stands out."""
    return f"{n:,}"


def _chaves_nulas(tabela_arrow, chaves: tuple[str, ...]) -> dict[str, int]:
    """NULL count per key column. Kept apart from `_colisoes` on purpose: `group_by` treats NULL as
    a normal grouping value, so a single row with a null key goes unnoticed (group of size 1) and
    two or more are counted as a real duplicate - indistinguishable from the defect the guard
    exists to flag. It does not block publishing: blocking would refuse datasets over a record that
    no current consumer uses. What it feeds is promoting key columns to `required=True` /
    `identifier-field-ids`, which a null key prevents."""
    import pyarrow.compute as pc

    nulas = {}
    for c in chaves:
        n = pc.sum(pc.is_null(tabela_arrow.column(c))).as_py() or 0
        if n:
            nulas[c] = int(n)
    return nulas


def _colisoes(tabela_arrow, chaves: tuple[str, ...]) -> tuple[int, int]:
    """`(colliding rows, groups)` for the key columns. Zero means the grain is respected.

    Groups in Arrow, not SQL: the table is already fully in memory (PyIceberg requires it) and
    opening DuckDB just to count duplicates would cost a second copy."""
    import pyarrow as pa
    import pyarrow.compute as pc

    agrupado = (
        tabela_arrow.select(list(chaves)).group_by(list(chaves)).aggregate([([], "count_all")])
    )
    contagens = agrupado.column("count_all")
    repetidos = pc.greater(contagens, 1)
    grupos = pc.sum(pc.cast(repetidos, pa.int64())).as_py() or 0
    linhas = pc.sum(pc.filter(contagens, repetidos)).as_py() or 0
    return int(linhas), int(grupos)


def conferir_grao(
    *, dataset: str, tabela_arrow: pa.Table, grao: dict | None
) -> tuple[Grao, dict[str, int]]:
    """Refuse publishing when the DECLARED grain does not identify the row. Returns the grain read and
    the null-key count per column - which does NOT block, it is only measured (see
    `_chaves_nulas`).

    `grao` is the block the output node carries (`config.grao`), already using silver column
    names.

    State `desconhecido` or `sem_chave` **does not block** - blocking there would stop publishing
    for every dataset whose declaration nobody has measured yet. They are recorded in the snapshot
    (`cflow.grao`), where `historico()` exposes them, and become a finding in the checker."""
    g = ler_grao({"grao": grao})
    if not g.declarado:
        return g, {}

    ausentes = [c for c in g.chaves if c not in tabela_arrow.schema.names]
    if ausentes:
        raise ErroAplicacao(
            codigo="silver_grao_sem_coluna",
            mensagem=(
                f"Silver '{dataset}': the declared grain uses {', '.join(ausentes)}, "
                "which do(es) not exist in the pipeline result. Publishing refused."
            ),
            status=409,
        )

    nulas = _chaves_nulas(tabela_arrow, g.chaves)
    linhas, grupos = _colisoes(tabela_arrow, g.chaves)
    if linhas:
        excedentes = linhas - grupos
        aviso_nulas = (
            f" ({_mil(sum(nulas.values()))} null-key row(s) in "
            f"{', '.join(sorted(nulas))} count toward this total.)"
            if nulas
            else ""
        )
        raise ErroAplicacao(
            codigo="silver_grao_violado",
            mensagem=(
                f"Silver '{dataset}': the declared grain ({', '.join(g.chaves)}) does not identify "
                f"the row - {_mil(linhas)} of {_mil(tabela_arrow.num_rows)} row(s) repeat the "
                f"key in {_mil(grupos)} group(s), {_mil(excedentes)} excess row(s)."
                f"{aviso_nulas} Publishing refused: fix the deduplication in the pipeline or the "
                "grain declared in the source contract."
            ),
            status=409,
        )
    return g, nulas


async def publicar(
    *,
    lake: Armazenamento,
    dataset: str,
    tabela_arrow: pa.Table,
    propriedades: dict[str, Any] | None = None,
    grao: dict | None = None,
    tokenizador: tokenizacao.Tokenizador | None = None,
) -> dict:
    """Write a new silver snapshot. Returns what identifies the published version.

    **Replaces the content, preserves history**: each run recomputes the whole silver from the
    immutable bronze, so the right operation is `overwrite` - which in Iceberg deletes nothing, it
    creates a new snapshot. Earlier versions stay queryable through time travel, which is exactly
    what is wanted.

    `grao` is the output node `config.grao`: when declared, publishing is **refused** if the key
    does not identify the row (see `conferir_grao`). The check comes BEFORE opening the catalog - a
    guard that runs after the data is in the bucket is not a guard, it is a report.
    """
    _exigir_slug(dataset)
    # BEFORE anything else: if the table has a column classified as personal data, the identifier
    # is replaced by a token here, and this point guarantees that no path publishes in clear text.
    # Without a tokenizer and with a classified column, publishing is REFUSED -
    # `tokenizacao.tokenizar_tabela` raises `pii_sem_tokenizador`.
    #
    # Why here and not in the pipeline SQL: HMAC needs a key that lives in a KMS, and DuckDB does
    # not speak KMS. Why before `conferir_grao`: if a classified column ever enters a grain key,
    # what is checked must be the data that will be published - HMAC is injective over distinct
    # values, so uniqueness does not change, but checking one table and publishing another is the
    # class of bug you cannot find by looking at the result.
    tabela_arrow = await tokenizacao.tokenizar_tabela(
        dataset=dataset, tabela=tabela_arrow, tokenizador=tokenizador
    )
    g, nulas = conferir_grao(dataset=dataset, tabela_arrow=tabela_arrow, grao=grao)
    cat = await abrir_catalogo(lake)
    ident = f"{NAMESPACE}.{dataset}"

    if cat.table_exists(ident):
        tabela = cat.load_table(ident)
        if _schema_mudou(tabela, tabela_arrow.schema):
            # This is why Iceberg was chosen: typing a column does not force rewriting the dataset
            # or resynchronising external tables - the change is transactional and versioned.
            def _evoluir(t):
                with t.update_schema(allow_incompatible_changes=True) as up:
                    up.union_by_name(tabela_arrow.schema)

            await _commitar_com_retry(_evoluir, lambda: cat.load_table(ident), dataset=dataset)
            tabela = cat.load_table(ident)
        nova = False
    else:
        tabela = cat.create_table(ident, schema=tabela_arrow.schema)
        nova = True

    props = {k: str(v) for k, v in (propriedades or {}).items()}
    # The grain state ALWAYS goes in the snapshot, including `desconhecido`: without the property,
    # whoever reads the history cannot tell "nobody declared it" from "published by a code version
    # that did not even ask". `historico()` returns the whole summary, so this shows up there.
    props["cflow.grao"] = g.estado
    if g.chaves:
        props["cflow.grao_chaves"] = ",".join(g.chaves)
    if g.motivo:
        props["cflow.grao_motivo"] = g.motivo
    if g.desempate:
        props["cflow.grao_desempate"] = ",".join(f"{c}:{o}" for c, o in g.desempate)
    if nulas:
        # This makes promoting columns to `required=True`/`identifier-field-ids` automatable: the
        # step that promotes a column reads this property and aborts with a named error instead of
        # discovering the violation in the middle of PyIceberg's `update_schema`.
        props["cflow.grao_chaves_nulas"] = ",".join(f"{c}={n}" for c, n in sorted(nulas.items()))
    if nova:
        # On a freshly created table, `overwrite` emits a `delete` that matches nothing - a useless
        # snapshot in the history and a warning that alarms whoever reads the log.
        tabela.append(tabela_arrow, snapshot_properties=props)
    else:
        await _commitar_com_retry(
            lambda t: t.overwrite(tabela_arrow, snapshot_properties=props),
            lambda: cat.load_table(ident),
            dataset=dataset,
        )
    tabela = cat.load_table(ident)
    snap = tabela.current_snapshot()
    return {
        "dataset": dataset,
        "tabela": ident,
        "snapshot_id": str(snap.snapshot_id) if snap else None,
        "momento_ms": snap.timestamp_ms if snap else None,
        "metadata_location": tabela.metadata_location,
        "location": tabela.location(),
        "linhas": tabela_arrow.num_rows,
        "colunas": list(tabela_arrow.schema.names),
    }


async def metadata_de(lake: Armazenamento, dataset: str) -> str:
    """Path of the current `metadata.json` - what DuckDB and Trino need to open the table. It comes
    from the catalog, never from a glob over the bucket."""
    _exigir_slug(dataset)
    cat = await abrir_catalogo(lake)
    ident = f"{NAMESPACE}.{dataset}"
    if not cat.table_exists(ident):
        raise ErroAplicacao(
            codigo="iceberg_tabela_ausente",
            mensagem=f"Table '{ident}' has not been published to silver yet.",
            status=400,
        )
    return cat.load_table(ident).metadata_location


async def historico(lake: Armazenamento, dataset: str) -> list[dict]:
    """One entry per PUBLICATION, oldest to newest - the version index loose Parquet never had.

    It is not one-to-one with snapshots: `overwrite` emits **two** (a `delete` and an `append`), and
    answering "3 versions" for someone who published twice would be a lie. Entries are grouped by
    `cflow.execucao_id` and the last one of each is kept - the snapshot where the table is
    complete."""
    _exigir_slug(dataset)
    cat = await abrir_catalogo(lake)
    tabela = cat.load_table(f"{NAMESPACE}.{dataset}")

    por_execucao: dict[str, dict] = {}
    for s in tabela.snapshots():
        operacao = s.summary.operation.value if s.summary else None
        # The `delete` of an overwrite is never a final state - it is dropped from the history. Our
        # flow only does append and overwrite, so there is no legitimate delete to hide.
        if operacao == "delete":
            continue
        # `additional_properties` and not `dict(summary)`: PyIceberg's Summary is not dict-like
        # despite appearances - iterating it yields tuples and breaks on key access.
        resumo = dict(s.summary.additional_properties) if s.summary else {}
        chave = resumo.get("cflow.execucao_id") or str(s.snapshot_id)
        por_execucao[chave] = {
            "snapshot_id": str(s.snapshot_id),
            "momento_ms": s.timestamp_ms,
            "execucao_id": resumo.get("cflow.execucao_id"),
            "operacao": operacao,
            "resumo": resumo,
        }
    return sorted(por_execucao.values(), key=lambda v: v["momento_ms"])
