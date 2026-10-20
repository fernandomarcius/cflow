"""Lake reads for a pipeline: resolve WHICH parquets to read and hand them to DuckDB.

This is the plumbing of the silver layer. What the specialist does with the data (typing,
deduplicating, conforming) is SQL in the graph - here we only resolve where the bytes come from
and where the output goes.

Two choices underpin the rest:

- **The catalog is the Postgres index, not a glob over the bucket.** `repositorio.lotes_bronze` is
  the only place that knows which version of each slice is valid. A backend `listar()` would
  return both versions of a republished slice - the wrong semantics.
- **Bytes come THROUGH THE BACKEND**, never through `httpfs`/`gs://`, so as not to open a second
  credential door outside the secret store. The cost is paid back with a cache: the bronze path is
  already content-addressed and the layer is immutable, so the cache never goes stale.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text

from ..config import get_settings
from ..db import transacao, trava_consultiva
from ..errors import ErroAplicacao
from ..io_lake import em_thread_lake
from ..timezone import FUSO_SAO_PAULO, agora_utc, para_exibicao
from . import repositorio as repo
from .armazenamento import Armazenamento, abrir
from .contrato import depende_da_fatia, fatia_e_competencia

PONTEIRO = "_atual.json"
MANIFESTO = "_manifesto.json"
# IMMUTABLE index of a run - the copy of `<dataset>/_atual.json` that the next run does not erase.
INDICE = "_indice.json"
# Key that marks a scope pointer whose PARTITION LAYOUT changed. The object stays readable (nothing
# disappears from the bucket), but it stops being served as a publication: without it, an unmigrated
# reader would receive the snapshot frozen on the migration day, forever and without any error -
# exactly the defect partitioning exists to kill, re-entering through the old pointer.
MARCA_OBSOLETO = "obsoleto"

# Name of the competence column projected when reading bronze. No `_` prefix here because the
# silver generator adds the prefix (`COLUNAS_PROVENIENCIA`), as it already does for `ingestion_date`.
COLUNA_COMPETENCIA = "competencia"


@dataclass(frozen=True)
class ParteLake:
    """A lake Parquet ready for DuckDB to open."""

    chave: str
    caminho: str
    uri: str
    sha256: str
    linhas: int | None


# --- Resolution (index only; touches no bytes) -------------------------------------------------


async def resolver_fonte(cfg: dict) -> dict:
    """Load the ingestion source referenced by `config.fonte_id`. A named 404 - without it the
    error would surface as a `NoneType` in the middle of the run."""
    fid = cfg.get("fonte_id")
    if not fid:
        raise ErroAplicacao(
            codigo="lake_sem_fonte",
            mensagem="A lake source requires 'fonte_id' (the ingestion dataset to read).",
            status=400,
        )
    async with transacao() as conn:
        r = (
            (
                await conn.execute(
                    text(
                        "SELECT id, dataset, destino_conexao_id, contrato "
                        "FROM ingestao_fontes WHERE id = :i"
                    ),
                    {"i": uuid.UUID(str(fid))},
                )
            )
            .mappings()
            .first()
        )
    if r is None:
        raise ErroAplicacao(
            codigo="lake_fonte_desconhecida",
            mensagem="The referenced ingestion source does not exist.",
            status=404,
        )
    return dict(r)


async def resolver(cfg: dict, *, parametros: dict | None = None) -> tuple[dict, list[dict]]:
    """`(source, batches)` - what exists to read, without downloading anything.

    `chave_min`/`chave_max` accept `:parameter`, so the cut becomes a run-time decision (for
    example, closing silver up to the report competence) instead of being stuck in the
    definition."""
    fonte = await resolver_fonte(cfg)
    lotes = await repo.lotes_bronze(
        fonte["id"],
        chave_min=_valor(cfg.get("chave_min"), parametros),
        chave_max=_valor(cfg.get("chave_max"), parametros),
    )
    lotes = _so_a_captura_mais_recente(fonte, lotes)
    if not lotes:
        # With no batch there is not even a way to know the columns: a silent empty table would make
        # the whole pipeline "work" while producing nothing.
        raise ErroAplicacao(
            codigo="lake_sem_lotes",
            mensagem=(
                f"Dataset '{fonte['dataset']}' has no bronze batch for the requested range. "
                "Run the ingestion of that source first."
            ),
            status=400,
        )
    return fonte, lotes


def _so_a_captura_mais_recente(fonte: dict, lotes: list[dict]) -> list[dict]:
    """Fixed-URL source: each slice is the WHOLE dataset, so only the last one counts.

    Files such as `registry.csv` are overwritten by the origin - it keeps no history. For these
    sources the partition is the CAPTURE date, not the data date (this is what
    `contrato.depende_da_fatia` detects), and each capture writes **a full copy of the dataset**
    under a new key.

    `lotes_bronze` deduplicates by `chave_lote` - one version per slice, which is right for a series
    in which each slice is a piece of time. Not here: the slices do not add up, they repeat. Reading
    all of them stacks the same data N times, where N is the number of captures already made.

    On a production-like host, right after the monthly reload created the second capture, every
    fixed-URL source showed exactly this: twice the rows in silver as in the latest capture, and
    the factor grows (x3 the next month, x4 the next). Without this cut, republishing any of them
    would double the silver.

    The cut is NOT "today's slice": it is the **largest key present in the requested range**. This
    way `chave_max` still works for looking at a past capture (which is what makes the check
    repeatable), instead of every range always returning the current capture.

    For every source with real history this is a no-op - `depende_da_fatia` returns `True` and the
    list comes out untouched.
    """
    contrato = fonte.get("contrato") or {}
    # The cut requires the contract to ASSERT that the origin has no history. `depende_da_fatia`
    # decides by looking at the `acesso` placeholders; in a contract without `acesso` it has nothing
    # to look at and returns `False` - which here would read as "fixed URL" and silently drop a good
    # slice. Silently losing data is worse than summing too much: the excess is flagged by the gate,
    # the shortfall is seen by nobody.
    if not contrato.get("acesso") or depende_da_fatia(contrato) or len(lotes) <= 1:
        return lotes
    ultima = max(lote["chave_lote"] for lote in lotes)
    return [lote for lote in lotes if lote["chave_lote"] == ultima]


def competencia_expandida(nome: str, parametros: dict, tipos_parametro: dict | None = None) -> bool:
    """Is parameter `nome` of type `competencia` and did it arrive EXPANDED (`_inicio`/`_fim`)?

    Normalisation (`definicao.validar_parametros`) does not inject `:competencia`: it expands into
    `:competencia_inicio`/`:competencia_fim`, which is what the SQL references. With the type map,
    only the type decides - an optional blank `texto` must not inherit the value of a neighbour that
    happens to be called `<name>_inicio`. Without the map, the expansion signature (BOTH sides)
    decides.

    It lives HERE, and not in the `engine`, because WRITING and READING must name the same scope:
    publishing uses it through `_particao_de_parametro` and the `publicado` source through
    `_valor`. While only the writer knew it, a source that read `{"competencia": ":competencia"}`
    resolved to empty and the prefix came out as `competencia=` - `lake_dataset_ausente` on every
    read, on the first run after the competence entered the partition.
    """
    if f"{nome}_inicio" not in parametros:
        return False
    if tipos_parametro is not None:
        return tipos_parametro.get(nome) == "competencia"
    return f"{nome}_fim" in parametros


def _valor(bruto, parametros: dict | None):
    """`:name` becomes the parameter value; anything else passes through. A parameter without a value
    means no cut.

    A `competencia` parameter does not exist under its raw name after normalisation - only expanded
    - and the scope uses the START of the window, the same rule as `_particao_de_parametro`."""
    if isinstance(bruto, str) and bruto.startswith(":"):
        nome = bruto[1:]
        ps = parametros or {}
        if ps.get(nome) is None and competencia_expandida(nome, ps):
            return ps[f"{nome}_inicio"] or None
        return ps.get(nome) or None
    return bruto or None


def assinatura(lotes: list[dict]) -> str:
    """Fingerprint of the SET of resolved batches.

    It enters the preview cache key: without it, a new ingestion would not invalidate the sample
    and the specialist would keep looking at stale data without having changed anything in the
    definition."""
    bruto = "|".join(f"{x['chave_lote']}:{x['sha256']}" for x in lotes)
    return hashlib.sha256(bruto.encode()).hexdigest()[:16]


# --- Materialisation (this is where bytes are touched) -----------------------------------------


def _cache_de(caminho_rel: str) -> Path:
    """Cache mirroring the object PATH in the bucket, not the batch `sha256`.

    The batch sha is that of the payload DOWNLOADED FROM THE ORIGIN, not of the written Parquet. A
    ZIP can contain nine tables of different grains - nine bronze datasets, nine distinct Parquets,
    and **a single sha**. Keyed by sha, the cache served the first downloaded file to all nine: one
    dataset read the content of another and the generator produced the same schema for tables
    that have nothing in common. The object in the bucket was always right; the read was the one
    confusing them.

    The object path is already unique per dataset, slice and content, and the layer is immutable -
    so the cache still never needs invalidating."""
    raiz = Path(get_settings().lake_cache_dir) / "bronze"
    destino = (raiz / caminho_rel).resolve()
    if not destino.is_relative_to(raiz.resolve()):
        raise ErroAplicacao(
            codigo="caminho_de_lote_invalido",
            mensagem="The batch path in the lake escapes the cache root.",
            status=400,
        )
    return destino


async def partes(
    cfg: dict, *, parametros: dict | None = None, limite_fatias: int | None = None
) -> tuple[list[ParteLake], dict]:
    """Resolve and materialise. `limite_fatias` (preview) takes the N MOST RECENT slices - a sample
    of today's data says more than one from years ago, and downloading 32 slices to show 10 rows is
    absurd."""
    fonte, lotes = await resolver(cfg, parametros=parametros)
    total = len(lotes)
    if limite_fatias is not None and limite_fatias < total:
        lotes = lotes[-limite_fatias:]

    lake = await abrir(fonte.get("destino_conexao_id"))
    out: list[ParteLake] = []
    for lote in lotes:
        caminho_rel = _relativo(lake, lote["bronze_uri"], "bronze")
        local = await lake.materializar("bronze", caminho_rel, _cache_de(caminho_rel))
        out.append(
            ParteLake(
                chave=lote["chave_lote"],
                caminho=local,
                uri=lote["bronze_uri"],
                sha256=lote["sha256"],
                linhas=lote["linhas"],
            )
        )
    contrato = fonte.get("contrato") or {}
    if isinstance(contrato, str):  # jsonb may arrive as text depending on the driver
        contrato = json.loads(contrato)
    resumo = {
        "dataset": fonte["dataset"],
        "fatias_lidas": len(out),
        "fatias_totais": total,
        "chaves": [p.chave for p in out][:20],
        "linhas_no_indice": sum(p.linhas or 0 for p in out),
        "fatia_e_competencia": fatia_e_competencia(contrato),
    }
    return out, resumo


def _relativo(lake: Armazenamento, uri: str, camada: str) -> str:
    """Absolute URI -> path INSIDE the layer. Refuses a batch from ANOTHER lake: the index may hold
    URIs of an old destination, and reading them would give "object missing" without saying why."""
    raiz = lake.raiz_uri(camada)
    if not uri or not uri.startswith(raiz):
        raise ErroAplicacao(
            codigo="lote_de_outro_lake",
            mensagem=(
                "The batch was written to another lake destination. Run the ingestion of this "
                "source again for the current destination."
            ),
            status=400,
        )
    return uri[len(raiz) :]


def sql_leitura(
    partes_: list[ParteLake], *, limite: int | None = None, competencia: bool = False
) -> tuple[str, list]:
    """`(sql, binds)` to materialise the parts into a DuckDB table.

    Paths go by BIND, never interpolated: they come from the database. `union_by_name` because the
    column order changes between years in a historical series - and it is safe precisely because
    bronze is all VARCHAR, so no type conflict is possible.

    `competencia=True` projects the slice key as a column. Without it, a daily source stacks 121
    days into a table where nothing says which day each row belongs to: that is how a daily source
    ended up with thousands of byte-for-byte identical rows that are **not** duplicates, just the
    same instrument on different days.

    **The key comes from the INDEX, not from the path.** `ParteLake.chave` is the `chave_lote` that
    `repositorio.lotes_bronze` loaded from the catalog - "the only place that knows which version of
    each slice is valid" - and it is `NOT NULL` in the database. It enters as a BOUND LITERAL in one
    subquery per part, joined by `UNION ALL BY NAME`. The previous version re-derived it from the
    filesystem with `regexp_extract` over `filename`, which coupled the reader to the bronze layout
    and admitted `NULL` as an outcome (hence the `NULLIF`, and the doubt "is it '' or 'unknown'?").
    With the value coming from the index there is no path string comparison, so the class of bug
    "did not match and silently returned zero rows" does not exist, and `NULL` stops being a
    possible outcome. Pinned by `core/tests/test_ingestao_competencia.py`.

    COST: one subquery per part when `competencia=True` (a daily source reaches ~121 slices in a
    monthly window). They cannot be grouped: `lotes_bronze` does `DISTINCT ON (chave_lote)`, so the
    part<->key relation is 1:1 and each part needs its own literal. Without `competencia` the SQL
    remains a single `read_parquet` over the whole list."""
    caminhos = [p.caminho for p in partes_]
    if competencia and partes_:
        # `CAST(? AS VARCHAR)`: without the explicit type DuckDB cannot infer the type of a loose
        # parameter in the projection list. `UNION ALL BY NAME` does what `union_by_name` did across
        # files - it matches by name and fills with NULL what is missing in one of the parts.
        # Bind order = order of appearance in the text: key, then path, per part.
        sql = " UNION ALL BY NAME ".join(
            f"SELECT *, CAST(? AS VARCHAR) AS {COLUNA_COMPETENCIA} "
            "FROM read_parquet(?, union_by_name := true)"
            for _ in partes_
        )
        binds: list = [x for p in partes_ for x in (p.chave, p.caminho)]
    else:
        sql = "SELECT * FROM read_parquet(?, union_by_name := true)"
        binds = [caminhos]
    if limite is not None:
        sql += f" LIMIT {int(limite)}"
    return sql, binds


# --- Partition of the published dataset --------------------------------------------------------

# The `as_of` reaches the node already resolved as 'YYYY-MM-DD HH:MM:SS' (`periodos.resolver_corte`)
# and a date column arrives as date/datetime. Without reducing to the DAY, the same snapshot of
# Aug 20 would come out sometimes as `record_date=2026-08-20` and sometimes as
# `record_date=2026-08-20 23:59:59`: two scopes for a single scope, and the consumer asking for "the
# snapshot of that day" would find half the data.
_TS_ATE_O_DIA = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T]\d{2}:\d{2}")
# The value becomes a PATH SEGMENT in the bucket: `/` and `\` would write outside the dataset scope
# and `=` would break the hive reading (`key=value`) of the segment itself.
_PROIBIDOS_NO_CAMINHO = ("/", "\\", "=", "\n", "\r", "\x00")
_CHAVE_PARTICAO = re.compile(r"^[a-z0-9_]+$")
# Value cap, in BYTES (not characters - the path segment is measured in bytes in both backends).
# Without it the value passed validation, the fan-out started writing and the one rejecting was the
# system `open()`: a raw `OSError [Errno 63] File name too long` on screen, with scopes already
# published. And the limit DIVERGED per backend - 255 bytes per segment on POSIX, 1024 bytes for the
# whole object name on GCS - so the same pipeline was accepted in one place and refused in the other.
# 128 fits comfortably in `key=value` inside the POSIX 255 and leaves room for several segments
# before the GCS 1024; a partition value larger than that is a label, not a scope key.
_TETO_VALOR_BYTES = 128


def texto_particao(valor) -> str:
    """STABLE textual form of a value that characterises the publication (path segment and
    manifest entry). Same function in both because the manifest must describe the scope the path
    states: if they diverged, whoever reads the manifest and rebuilds the path lands elsewhere."""
    if isinstance(valor, datetime):
        # `.date()` without converting the timezone on purpose: converting would shift the day and
        # the Aug 20 snapshot would show up as Aug 19 in the path.
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    texto = "" if valor is None else str(valor).strip()
    m = _TS_ATE_O_DIA.match(texto)
    return m.group(1) if m else texto


def valor_particao(chave: str, valor) -> str:
    """Like `texto_particao`, but REFUSES what cannot become a path. A loud refusal, never
    sanitising: a value silently "fixed" publishes the data under a scope that is not its own, and
    the error only shows up in the check, months later, when nobody connects one thing to the
    other."""
    texto = texto_particao(valor)
    if not texto:
        raise ErroAplicacao(
            codigo="particao_valor_invalido",
            mensagem=(
                f"Partition '{chave}': empty or null value. The path would become '{chave}=' and "
                "no hive partition reader knows what to do with that."
            ),
            status=400,
        )
    if texto in (".", "..") or any(c in texto for c in _PROIBIDOS_NO_CAMINHO):
        raise ErroAplicacao(
            codigo="particao_valor_invalido",
            mensagem=(
                f"Partition '{chave}': value {texto!r} cannot become a path in the lake "
                "(it contains '/', '\\', '=' or is '..'). Fix the data at the source."
            ),
            status=400,
        )
    tamanho = len(texto.encode("utf-8"))
    if tamanho > _TETO_VALOR_BYTES:
        raise ErroAplicacao(
            codigo="particao_valor_invalido",
            mensagem=(
                f"Partition '{chave}': the value is too long ({tamanho} bytes; the limit is "
                f"{_TETO_VALOR_BYTES}). A path segment larger than that is refused by the file "
                "system or the bucket, each at its own limit."
            ),
            status=400,
        )
    return texto


def normalizar_particao(particao: dict) -> dict[str, str]:
    """Validate key and value with the same contract the pipeline publication uses.

    The canonical return value must be used both to detect collisions and to build the path:
    validating one form and persisting another reopens the traversal the validation meant to close.
    """
    if not isinstance(particao, dict) or not particao:
        raise ErroAplicacao(
            codigo="particao_invalida",
            mensagem="Partition must be a non-empty object.",
            status=400,
        )
    normalizada: dict[str, str] = {}
    for chave, valor in particao.items():
        if not isinstance(chave, str) or not _CHAVE_PARTICAO.fullmatch(chave):
            raise ErroAplicacao(
                codigo="particao_invalida",
                mensagem=(
                    f"Invalid partition key: {chave!r} (use only lowercase letters, digits and '_')."
                ),
                status=400,
            )
        if chave in normalizada:
            raise ErroAplicacao(
                codigo="particao_invalida",
                mensagem=f"The key '{chave}' appears more than once in the scope.",
                status=400,
            )
        normalizada[chave] = valor_particao(chave, valor)
    return normalizada


# Parameter whose value IS a day: `as_of` arrives resolved as 'YYYY-MM-DD HH:MM:SS'
# (`periodos.resolver_corte` appends the end of the day) and `data` is a day by definition. For
# these the day is the value; for everything else, the time is information.
_TIPOS_DE_DIA = ("as_of", "data")


def texto_parametro(valor, tipo: str | None = None):
    """Textual form of a parameter for the MANIFEST and the INDEX, normalised by the DECLARED TYPE.

    `as_of`/`data` become the day (`as_of` arrives with the end of day appended by the period
    resolver: writing '2026-08-20 23:59:59' where the person asked for '2026-08-20' would describe
    the run by an implementation detail). ANY OTHER type goes verbatim.

    The distinction is by type and not by a regex over the text because the manifest is IMMUTABLE: a
    `texto` parameter that looks like a timestamp - a cut-off, a stamp coming from the source
    system - would lose its time forever, in an object that cannot be fixed afterwards, without even
    partitioning anything. The scope is still described unambiguously in `particao`, which is what
    the path states."""
    if isinstance(valor, datetime | date):
        return valor.date().isoformat() if tipo in _TIPOS_DE_DIA else valor.isoformat()
    if valor is None:
        return None
    texto = str(valor)
    if tipo in _TIPOS_DE_DIA:
        m = _TS_ATE_O_DIA.match(texto.strip())
        return m.group(1) if m else texto.strip()
    return texto


AMBIENTES_LAKE = frozenset({"dev", "prd"})
CHAVE_AMBIENTE = "ambiente"


def ambiente_lake(parametros: dict | None, *, obrigatorio: bool = False) -> str | None:
    """Logical environment that selects the physical root of the lake.

    The environment is publication context, not a Hive key: the physical contract is
    ``<environment>/<dataset>/<dataset partitions>``. This keeps manifest, index and aggregate with
    the same ``particao`` and prevents a DEV pointer from being shared with PRD.
    """
    valor = (parametros or {}).get(CHAVE_AMBIENTE)
    if valor is None and not obrigatorio:
        return None
    if not isinstance(valor, str) or valor not in AMBIENTES_LAKE:
        raise ErroAplicacao(
            codigo="ambiente_lake_invalido",
            mensagem="Provide environment 'dev' or 'prd' to access the gold layer.",
            status=400,
        )
    return valor


def ambiente_da_camada(
    camada: str = "gold",
    parametros: dict | None = None,
    *,
    ambiente: str | None = None,
    obrigatorio_gold: bool = False,
) -> str | None:
    """Apply the environment namespace to Gold only.

    Silver predates the DEV/PRD isolation and its paths are a persisted contract. Even if a run
    carries ``parametros.ambiente``, that context cannot change the physical address of a Silver
    publication.
    """
    if camada != "gold":
        return None
    declarado = ambiente_lake(parametros, obrigatorio=False)
    if ambiente is not None:
        explicito = ambiente_lake({CHAVE_AMBIENTE: ambiente}, obrigatorio=True)
        if declarado is not None and declarado != explicito:
            raise ErroAplicacao(
                codigo="ambiente_lake_divergente",
                mensagem="The parameters environment differs from the given environment.",
                status=409,
            )
        declarado = explicito
    if declarado is None and obrigatorio_gold:
        ambiente_lake({}, obrigatorio=True)
    return declarado


def separar_ambiente_particao(
    particao: dict[str, str] | None, *, ambiente: str | None = None
) -> tuple[str | None, dict[str, str]]:
    """Accept the logical pipeline contract and return only the physical partition of the dataset."""
    normalizada = normalizar_particao(particao) if particao else {}
    embutido = normalizada.pop(CHAVE_AMBIENTE, None)
    if embutido is not None:
        if embutido not in AMBIENTES_LAKE:
            raise ErroAplicacao(
                codigo="ambiente_lake_invalido",
                mensagem="The logical partition declares an invalid environment.",
                status=400,
            )
        if ambiente is not None and ambiente != embutido:
            raise ErroAplicacao(
                codigo="ambiente_lake_divergente",
                mensagem="The parameters environment differs from the logical partition environment.",
                status=409,
            )
        ambiente = embutido
    return ambiente, normalizada


def prefixo_dataset(dataset: str, ambiente: str | None = None) -> str:
    if ambiente is None:
        return dataset
    if ambiente not in AMBIENTES_LAKE:
        raise ErroAplicacao(
            codigo="ambiente_lake_invalido",
            mensagem="The lake root only accepts the 'dev' and 'prd' environments.",
            status=400,
        )
    return f"{ambiente}/{dataset}"


def prefixo_escopo(
    dataset: str,
    particao: dict[str, str] | None,
    *,
    ambiente: str | None = None,
) -> str:
    """Environment root + dataset + physical Hive partitions of the dataset."""
    ambiente, fisica = separar_ambiente_particao(particao, ambiente=ambiente)
    prefixo = prefixo_dataset(dataset, ambiente)
    return prefixo + "".join(f"/{c}={v}" for c, v in fisica.items())


def prefixo_imutavel_no_ambiente(dataset: str, prefixo_imutavel: str, ambiente: str | None) -> str:
    """Normalise the immutable ancestor without accepting an escape to another dataset/environment."""
    raiz = prefixo_dataset(dataset, ambiente)
    if prefixo_imutavel == raiz:
        return prefixo_imutavel
    if prefixo_imutavel.startswith(f"{raiz}/"):
        segmentos = prefixo_imutavel[len(raiz) + 1 :].split("/")
        if any(segmento.startswith(f"{CHAVE_AMBIENTE}=") for segmento in segmentos):
            raise ErroAplicacao(
                codigo="prefixo_imutavel_invalido",
                mensagem="The immutable prefix does not accept environment as a physical partition.",
                status=400,
            )
        return prefixo_imutavel
    if prefixo_imutavel == dataset:
        return raiz
    if prefixo_imutavel.startswith(f"{dataset}/"):
        segmentos = prefixo_imutavel[len(dataset) + 1 :].split("/")
        posicoes_ambiente = [
            i for i, segmento in enumerate(segmentos) if segmento.startswith(f"{CHAVE_AMBIENTE}=")
        ]
        if posicoes_ambiente and posicoes_ambiente != [0]:
            raise ErroAplicacao(
                codigo="prefixo_imutavel_invalido",
                mensagem="The legacy environment is only accepted as the first physical partition.",
                status=400,
            )
        if not posicoes_ambiente:
            return f"{ambiente}/{prefixo_imutavel}" if ambiente is not None else prefixo_imutavel
        primeiro_segmento = segmentos[0]
        ambiente_legado = primeiro_segmento.split("=", 1)[1]
        if ambiente is None or ambiente_legado != ambiente:
            raise ErroAplicacao(
                codigo="ambiente_lake_divergente",
                mensagem="The legacy immutable prefix belongs to another environment.",
                status=409,
            )
        restantes = segmentos[1:]
        return raiz + (f"/{'/'.join(restantes)}" if restantes else "")
    raise ErroAplicacao(
        codigo="prefixo_imutavel_invalido",
        mensagem="The immutable index escapes the dataset root.",
        status=400,
    )


def validar_documento_publicado(
    documento: dict,
    *,
    dataset: str,
    camada: str,
    ambiente: str | None,
    particao_esperada: dict[str, str] | None = None,
    execucao_id: str | uuid.UUID | None = None,
) -> None:
    """Refuse metadata that would let a reader escape the requested dataset, scope or environment."""
    ambiente = ambiente_da_camada(camada, ambiente=ambiente)
    if not isinstance(documento, dict):
        raise ErroAplicacao(
            codigo="lake_ponteiro_invalido",
            mensagem="The published document is not a JSON object.",
            status=409,
        )
    if documento.get("dataset") != dataset or documento.get("camada") != camada:
        raise ErroAplicacao(
            codigo="lake_ponteiro_invalido",
            mensagem="The published document differs from the requested dataset or layer.",
            status=409,
        )
    parametros_documento = documento.get("parametros")
    if camada == "gold":
        if (
            not isinstance(parametros_documento, dict)
            or parametros_documento.get(CHAVE_AMBIENTE) != ambiente
        ):
            raise ErroAplicacao(
                codigo="lake_ponteiro_invalido",
                mensagem="The Gold document differs from the requested environment.",
                status=409,
            )
    elif (
        isinstance(parametros_documento, dict)
        and CHAVE_AMBIENTE in parametros_documento
        and parametros_documento[CHAVE_AMBIENTE] not in AMBIENTES_LAKE
    ):
        raise ErroAplicacao(
            codigo="lake_ponteiro_invalido",
            mensagem="The Silver environment metadata is invalid.",
            status=409,
        )
    if execucao_id is not None and str(documento.get("execucao_id") or "") != str(execucao_id):
        raise ErroAplicacao(
            codigo="lake_ponteiro_invalido",
            mensagem="The published document differs from the requested run.",
            status=409,
        )

    def validar_item(item: dict, chaves: list[str] | None = None) -> None:
        if not isinstance(item, dict):
            raise ErroAplicacao(
                codigo="lake_ponteiro_invalido",
                mensagem="The published document contains an invalid partition.",
                status=409,
            )
        particao_bruta = item.get("particao")
        if particao_bruta is None and chaves is None and particao_esperada is not None:
            # Compatibility with old pointers: the path is still proven against the requested scope,
            # even when the older document did not repeat the key.
            particao_bruta = particao_esperada
        if not isinstance(particao_bruta, dict):
            raise ErroAplicacao(
                codigo="lake_ponteiro_invalido",
                mensagem="The published document contains an invalid partition.",
                status=409,
            )
        particao = normalizar_particao(particao_bruta) if particao_bruta else {}
        if camada == "gold" and CHAVE_AMBIENTE in particao:
            raise ErroAplicacao(
                codigo="lake_ponteiro_invalido",
                mensagem="Environment cannot be part of the Gold physical partition.",
                status=409,
            )
        if chaves is not None and list(particao) != chaves:
            raise ErroAplicacao(
                codigo="lake_ponteiro_invalido",
                mensagem="The published partition differs from the index keys.",
                status=409,
            )
        caminho = item.get("caminho")
        prefixo = prefixo_escopo(dataset, particao, ambiente=ambiente)
        if not isinstance(caminho, str) or f"/{CHAVE_AMBIENTE}=" in caminho:
            raise ErroAplicacao(
                codigo="lake_ponteiro_invalido",
                mensagem="The published path contains an invalid physical namespace.",
                status=409,
            )
        padrao = rf"{re.escape(prefixo)}/execucao_id=([^/]+)/dados\.parquet"
        match = re.fullmatch(padrao, caminho)
        if match is None or (execucao_id is not None and match.group(1) != str(execucao_id)):
            raise ErroAplicacao(
                codigo="lake_ponteiro_invalido",
                mensagem="The published path escapes the requested root, scope or run.",
                status=409,
            )

    if "caminho" in documento:
        validar_item(documento)
        recebido = documento.get("particao", particao_esperada) or {}
        if particao_esperada is not None and recebido != particao_esperada:
            raise ErroAplicacao(
                codigo="lake_ponteiro_invalido",
                mensagem="The published manifest differs from the requested partition.",
                status=409,
            )
        return

    chaves = documento.get("chaves")
    particoes = documento.get("particoes")
    if (
        not isinstance(chaves, list)
        or any(not isinstance(chave, str) for chave in chaves)
        or len(chaves) != len(set(chaves))
        or not isinstance(particoes, list)
        or (camada == "gold" and CHAVE_AMBIENTE in chaves)
    ):
        raise ErroAplicacao(
            codigo="lake_ponteiro_invalido",
            mensagem="The published index contains invalid keys or partitions.",
            status=409,
        )
    for item in particoes:
        validar_item(item, chaves)


# --- Publishing a derived dataset (silver / gold) ----------------------------------------------


def _sem_momento(documento: dict) -> dict:
    return {k: v for k, v in documento.items() if k != "momento"}


async def _gravar_json_immutavel_idempotente(
    lake: Armazenamento,
    camada: str,
    caminho: str,
    corpo: dict,
) -> None:
    """Create an immutable JSON or prove that the object left by a previous attempt is the same."""
    if await lake.existe(camada, caminho):
        try:
            existente = json.loads(await lake.ler(camada, caminho))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ErroAplicacao(
                codigo="publicacao_immutavel_divergente",
                mensagem="The existing immutable object does not contain valid JSON.",
                status=409,
            ) from exc
        if not isinstance(existente, dict) or _sem_momento(existente) != _sem_momento(corpo):
            raise ErroAplicacao(
                codigo="publicacao_immutavel_divergente",
                mensagem="The existing immutable object differs from the resumed attempt.",
                status=409,
            )
        return
    await lake.gravar(
        camada,
        caminho,
        json.dumps(corpo, ensure_ascii=False, indent=2).encode(),
        tipo_mime="application/json",
    )


async def _gravar_json_imutavel(lake: Armazenamento, camada: str, caminho: str, corpo: dict):
    """Write the immutable JSON in one round trip; if the PREVIOUS attempt of the same run already
    left it there (`objeto_ja_existe`: a workflow-engine retry after the pod died in the middle of
    publishing), prove it is the same - ignoring `momento` - and carry on. The happy path pays no
    extra round trip."""
    try:
        await lake.gravar(
            camada,
            caminho,
            json.dumps(corpo, ensure_ascii=False, indent=2).encode(),
            tipo_mime="application/json",
        )
    except ErroAplicacao as exc:
        if exc.codigo != "objeto_ja_existe":
            raise
        await _gravar_json_immutavel_idempotente(lake, camada, caminho, corpo)


async def _gravar_parquet_imutavel(
    lake: Armazenamento, camada: str, caminho: str, parquet: bytes | Path
) -> str:
    """Same principle as `_gravar_json_imutavel` for the data: the object left by the previous
    attempt is only accepted if it has the SAME bytes (sha256). A divergent one is a hard
    conflict."""
    try:
        if isinstance(parquet, Path):
            return await lake.gravar_arquivo(
                camada, caminho, parquet, tipo_mime="application/vnd.apache.parquet"
            )
        return await lake.gravar(
            camada, caminho, parquet, tipo_mime="application/vnd.apache.parquet"
        )
    except ErroAplicacao as exc:
        if exc.codigo != "objeto_ja_existe":
            raise
        local = await em_thread_lake(parquet.read_bytes) if isinstance(parquet, Path) else parquet
        existente = await lake.ler(camada, caminho)

        def _iguais() -> bool:
            return hashlib.sha256(existente).digest() == hashlib.sha256(local).digest()

        if not await em_thread_lake(_iguais):
            raise ErroAplicacao(
                codigo="publicacao_immutavel_divergente",
                mensagem="The existing immutable Parquet differs from the resumed attempt.",
                status=409,
            ) from exc
        return lake.uri(camada, caminho)


async def gravar_dataset(
    *,
    lake: Armazenamento,
    dataset: str,
    execucao_id,
    pipeline_id,
    no_id: str,
    parquet: bytes | Path,
    linhas: int,
    colunas: list[str],
    camada: str = "silver",
    particao: dict[str, str] | None = None,
    parametros: dict | None = None,
    tipos_parametro: dict[str, str] | None = None,
) -> dict:
    """Write the Parquet and the manifest at an IMMUTABLE path. Does NOT move the pointer - that is
    `mover_ponteiro`, and the separation is what gives the fan-out a commit point.

    Swapping `_atual.json` is the *commit point*: a crash before it leaves the previous version in
    force and the orphan is harmless garbage, never corruption. It is what lets the layer be
    rewritten without any data object ever being overwritten.

    `camada` is `silver` (the default) or `gold`. The mechanics are the SAME - what changes is the
    bucket and the meaning: silver is the typed bronze table, gold is a conformed product. The
    layer goes into the manifest because whoever reads a loose object in the bucket has no other way
    to know which contract it follows.

    `particao` is the scope of this publication ({key: value}, already validated by
    `valor_particao`): it becomes a hive segment of the path AND the pointer becomes PER SCOPE.
    Without it, the path and the pointer are exactly the previous ones. `parametros` go into the
    manifest because whoever only has the Parquet has nowhere else to find out which run that number
    came from."""
    ambiente = ambiente_da_camada(camada, parametros, obrigatorio_gold=camada == "gold")
    if camada == "gold":
        ambiente, particao_fisica = separar_ambiente_particao(particao, ambiente=ambiente)
    else:
        particao_fisica = normalizar_particao(particao) if particao else {}
    prefixo = prefixo_escopo(dataset, particao_fisica, ambiente=ambiente)
    base = f"{prefixo}/execucao_id={execucao_id}"
    momento = para_exibicao(agora_utc(), FUSO_SAO_PAULO).isoformat()
    manifesto = {
        "dataset": dataset,
        "camada": camada,
        "execucao_id": str(execucao_id),
        "pipeline_id": str(pipeline_id),
        "no_id": no_id,
        "particao": particao_fisica,
        # Verbatim (see `texto_parametro`) - and secret-free: the caller hands over the parameters
        # already redacted, because the manifest goes to the bucket, where nothing is erased later.
        "parametros": {
            k: texto_parametro(v, (tipos_parametro or {}).get(k))
            for k, v in (parametros or {}).items()
        },
        "linhas": linhas,
        "colunas": colunas,
        # From the file, when it is a file: reading the bytes just to measure them would defeat the
        # reason for accepting a `Path` here.
        "bytes": (os.stat(parquet).st_size if isinstance(parquet, Path) else len(parquet)),
        "momento": momento,
        "caminho": f"{base}/dados.parquet",
    }
    # A `Path` is published by LINK on the local backend (the Parquet does not go through the
    # process memory); `bytes` is the usual path, and what external ingestion uses - there the data
    # comes from the network.
    destino_dados = f"{base}/dados.parquet"
    caminho_manifesto = f"{base}/{MANIFESTO}"
    # Data and manifest are both IMMUTABLE and no consumer sees them before the pointer: the order
    # between them is not a contract, so they go up together. One storage round trip fewer per
    # publication - on GCS that is ~200-330 ms.
    # Waits for BOTH before deciding: with a bare `gather`, the failure of one would return control
    # while the other was still writing after the node was marked failed. Each accepts the SAME
    # object a previous attempt may have left: running together, the retry may find the manifest
    # landed and the data not.
    uri, manifesto_gravado = await asyncio.gather(
        _gravar_parquet_imutavel(lake, camada, destino_dados, parquet),
        _gravar_json_imutavel(lake, camada, caminho_manifesto, manifesto),
        return_exceptions=True,
    )
    for resultado in (uri, manifesto_gravado):
        if isinstance(resultado, BaseException):
            raise resultado
    return {**manifesto, "uri": uri, "prefixo": prefixo}


async def mover_ponteiro(
    *,
    lake: Armazenamento,
    camada: str,
    publicado: dict,
) -> str:
    """Swap the scope `_atual.json` for what `gravar_dataset` just wrote. It is the *commit
    point* - which is why it is a function SEPARATE from the write.

    In a per-project fan-out, publishing and committing in the same step has no commit point at
    all: if project 12 failed, the others were already pointing at a run marked as failed.
    Writing everything first (immutable path, which no consumer sees) and only then moving the
    pointers gives the run a before and an after."""
    corpo = {k: v for k, v in publicado.items() if k != "prefixo"}
    return await lake.gravar(
        camada,
        f"{publicado['prefixo']}/{PONTEIRO}",
        json.dumps(corpo, ensure_ascii=False, indent=2).encode(),
        tipo_mime="application/json",
        sobrescrever=True,
    )


async def publicar_dataset(**kw) -> dict:
    """`gravar_dataset` + `mover_ponteiro` in a single step - the usual form, for whoever publishes
    ONE object (silver) and has no sibling scopes that could fail halfway."""
    camada = kw.get("camada", "silver")
    publicado = await gravar_dataset(**kw)
    await mover_ponteiro(lake=kw["lake"], camada=camada, publicado=publicado)
    return {k: v for k, v in publicado.items() if k != "prefixo"}


# Old name: publishing was born exclusive to silver. Kept so as not to break external callers while
# the gold layer is new.
publicar_silver = publicar_dataset


async def publicar_indice(
    *,
    lake: Armazenamento,
    dataset: str,
    camada: str,
    execucao_id,
    pipeline_id,
    no_id: str,
    parametros: dict | None,
    chaves: list[str],
    particoes: list[dict],
    colunas: list[str],
    prefixo_imutavel: str,
    tipos_parametro: dict[str, str] | None = None,
    esperar=None,
) -> str | dict:
    """Write `<dataset>/_atual.json` as the RUN INDEX when the dataset is partitioned.

    `esperar` (an optional awaitable) is work that must finish BEFORE the immutable index - the
    scope pointers of the same publication. With it, that work and the read of `_atual.json` (under
    the lock) run together; then comes the immutable one and, last, the mutable one. One storage
    round trip fewer in series. If any step fails, `_atual.json` does not change.

    With a per-project fan-out, "the last parquet" means nothing - which of the 190? The index
    answers *which scopes this run produced and where*, which is the question asked from outside.
    Without it the answer would require listing the bucket, and `Armazenamento` has no `listar()`
    (nor should it get one: a glob would also return old runs, which are not "the current one").

    It also writes the SAME thing to `<prefixo_imutavel>/execucao_id=<uuid>/_indice.json`, which
    nobody overwrites. `<dataset>/_atual.json` is a single mutable path: the next run of ANY
    operation replaces it, and with it goes the only evidence of which scopes the previous run
    covered. When the same dataset is published per operation, after a nightly cycle the index
    would describe only the last one to finish, and a reconciliation check must be repeatable for a
    past date."""
    ambiente = ambiente_da_camada(camada, parametros, obrigatorio_gold=camada == "gold")
    chaves_fisicas = (
        [chave for chave in chaves if chave != CHAVE_AMBIENTE] if camada == "gold" else list(chaves)
    )
    if len(chaves_fisicas) != len(set(chaves_fisicas)):
        raise ErroAplicacao(
            codigo="particao_invalida",
            mensagem="The physical index keys must be unique.",
            status=400,
        )
    particoes_fisicas: list[dict] = []
    for item in particoes:
        item_fisico = dict(item)
        if camada == "gold":
            ambiente_item, particao_fisica = separar_ambiente_particao(
                item.get("particao"), ambiente=ambiente
            )
            if ambiente_item != ambiente:
                raise ErroAplicacao(
                    codigo="ambiente_lake_divergente",
                    mensagem="An index partition differs from the environment root.",
                    status=409,
                )
        else:
            particao_fisica = (
                normalizar_particao(item.get("particao")) if item.get("particao") else {}
            )
        item_fisico["particao"] = particao_fisica
        particoes_fisicas.append(item_fisico)
    prefixo_imutavel = prefixo_imutavel_no_ambiente(dataset, prefixo_imutavel, ambiente)
    corpo = {
        "dataset": dataset,
        "camada": camada,
        "execucao_id": str(execucao_id),
        "pipeline_id": str(pipeline_id),
        "no_id": no_id,
        "momento": para_exibicao(agora_utc(), FUSO_SAO_PAULO).isoformat(),
        "parametros": {
            k: texto_parametro(v, (tipos_parametro or {}).get(k))
            for k, v in (parametros or {}).items()
        },
        # The key order IS the path format: without publishing it, the consumer would have to infer
        # how the prefix is assembled from an example.
        "chaves": chaves_fisicas,
        "particoes": particoes_fisicas,
        # The columns of the published node, in its order - ALWAYS, even with `particoes: []`. With
        # `permitir_vazio` and column partitioning the run produces no scope at all, and whoever
        # reads the publication by run (`ingestao/publicados.py`) must still say which columns the
        # dataset has: with no parquet to open, the index is the only place to take that from. The
        # manifest of each scope already carried them; the index, which always exists, did not.
        "colunas": list(colunas),
    }
    # The immutable one FIRST: if the run dies between the two, the history exists and the mutable
    # pointer keeps describing the previous run - the reverse order would lose both.
    caminho_indice = f"{prefixo_imutavel}/execucao_id={execucao_id}/{INDICE}"
    gravar = _gravar_json_imutavel
    if esperar is None:
        await gravar(lake, camada, caminho_indice, corpo)
        return await mover_indice_atual(lake=lake, camada=camada, dataset=dataset, corpo=corpo)

    async def _antes_do_atual() -> None:
        # Scope pointers BEFORE the immutable index: an existing `_indice.json` still means the
        # publication of that run reached the pointers - whoever reads by `execucao_id` does not
        # find an index of a run that stopped halfway.
        await esperar
        await gravar(lake, camada, caminho_indice, corpo)

    # What precedes the mutable one starts NOW, outside the lock: waiting for the lock and reading
    # `_atual.json` run on top of it, and the mutable one is only written afterwards.
    antes_do_atual = asyncio.ensure_future(_antes_do_atual())
    try:
        return await mover_indice_atual(
            lake=lake, camada=camada, dataset=dataset, corpo=corpo, esperar=antes_do_atual
        )
    finally:
        # The read or the lock failed: whatever was already dispatched finishes before the node
        # fails, otherwise a write would continue after the publication is reported as failed.
        if not antes_do_atual.done():
            await asyncio.wait([antes_do_atual])
        if not antes_do_atual.cancelled():
            antes_do_atual.exception()


async def mover_indice_atual(
    *,
    lake: Armazenamento,
    camada: str,
    dataset: str,
    corpo: dict,
    esperar=None,
) -> str:
    """Merge and move the current index.

    `esperar`: an awaitable that must complete before the write (see `publicar_indice`). It is
    awaited INSIDE the lock, after the read - a failure in it leaves `_atual.json` as it was."""
    ambiente = ambiente_da_camada(
        camada, corpo.get("parametros"), obrigatorio_gold=camada == "gold"
    )
    raiz_dataset = prefixo_dataset(dataset, ambiente)
    # The mutable one is the CURRENT index PER SCOPE, not "the one of the last run": with a fan-out,
    # N runs of the same night publish DIFFERENT partitions of the same dataset at the same time,
    # and each one overwriting the whole `<dataset>/_atual.json` left the index describing only the
    # one that finished last - the other scopes vanished from it. It reads what is there, swaps the
    # partitions of this run, keeps the others, writes. Under a Postgres advisory lock, because every
    # writer is a worker of this platform sharing the same database; a CAS on the object generation
    # would only be needed with an outside writer.
    async with trava_consultiva(f"cflow:lake:{camada}:{raiz_dataset}"):
        visivel = await ler_ponteiro_bruto(lake, dataset, camada, ambiente=ambiente)
        if esperar is not None:
            await esperar
        atual = (
            visivel
            if isinstance(visivel, dict)
            and "particoes" in visivel
            and visivel.get("chaves") == corpo["chaves"]
            else None
        )
        mesclado = _mesclar_indice(atual, corpo)
        return await lake.gravar(
            camada,
            f"{raiz_dataset}/{PONTEIRO}",
            json.dumps(mesclado, ensure_ascii=False, indent=2).encode(),
            tipo_mime="application/json",
            sobrescrever=True,
        )


async def _ler_indice_atual(
    lake, camada: str, dataset: str, chaves: list[str], *, ambiente: str | None = None
) -> dict | None:
    """The current index, or None if there is none, it is not an index (it is the manifest of an
    unpartitioned dataset) or it has another path format (different `chaves` - a dataset that
    changed partitioning must not inherit scopes from a layout that no longer exists)."""
    try:
        atual = json.loads(
            await lake.ler(camada, f"{prefixo_dataset(dataset, ambiente)}/{PONTEIRO}")
        )
    except ErroAplicacao as e:
        if e.status == 404:
            return None
        raise
    if not isinstance(atual, dict) or "particoes" not in atual or atual.get("chaves") != chaves:
        return None
    return atual


def _chave_escopo(particao: dict) -> tuple:
    return tuple(sorted((str(k), str(v)) for k, v in (particao or {}).items()))


def _mesclar_indice(atual: dict | None, corpo: dict) -> dict:
    """`corpo` (this run) on top of `atual`: a scope of this run replaces its namesake; a scope that
    only `atual` has stays - it is ITS current pointer, moved by another run. The header fields
    (`execucao_id`, `parametros`, `momento`) are those of this run: they say who touched it last;
    the exact history of each run is in the immutable `_indice.json`."""
    if not atual:
        return corpo
    desta = {_chave_escopo(p.get("particao")): p for p in corpo["particoes"]}
    herdadas = [p for p in atual["particoes"] if _chave_escopo(p.get("particao")) not in desta]
    return {**corpo, "particoes": [*herdadas, *corpo["particoes"]]}


async def ler_ponteiro_bruto(
    lake: Armazenamento,
    dataset: str,
    camada: str,
    particao: dict[str, str] | None = None,
    *,
    ambiente: str | None = None,
) -> dict | None:
    """Pointer exactly as stored in the object, or None if it does not exist."""
    ambiente = ambiente_da_camada(camada, ambiente=ambiente)
    try:
        corpo = json.loads(
            await lake.ler(
                camada, f"{prefixo_escopo(dataset, particao, ambiente=ambiente)}/{PONTEIRO}"
            )
        )
    except ErroAplicacao as exc:
        if exc.status == 404:
            return None
        raise
    if not isinstance(corpo, dict):
        raise ErroAplicacao(
            codigo="lake_ponteiro_invalido",
            mensagem="The published pointer is not a JSON object.",
            status=409,
        )
    return corpo


async def ler_ponteiro(
    lake: Armazenamento,
    dataset: str,
    camada: str = "silver",
    particao: dict[str, str] | None = None,
    *,
    ambiente: str | None = None,
) -> dict:
    """What is current in the layer. It is the contract for the external consumer.

    Without `particao`, it reads `<dataset>/_atual.json` - which in a partitioned dataset is the run
    INDEX, not a manifest. For the manifest of one scope, give the `particao`."""
    ambiente = ambiente_da_camada(camada, ambiente=ambiente)
    if camada == "gold":
        ambiente, particao_fisica = separar_ambiente_particao(particao, ambiente=ambiente)
    else:
        particao_fisica = normalizar_particao(particao) if particao else {}
    corpo = await ler_ponteiro_bruto(lake, dataset, camada, particao_fisica, ambiente=ambiente)
    if corpo is None:
        raise ErroAplicacao(
            codigo="lake_dataset_ausente",
            mensagem=(f"Dataset '{dataset}' has not been published to layer '{camada}' yet."),
            status=400,
        )
    _recusar_ponteiro_obsoleto(dataset, camada, corpo)
    return corpo


def _recusar_ponteiro_obsoleto(dataset: str, camada: str, corpo) -> None:
    """A STAMPED pointer is not a publication - it is the trace of a partition layout that ended.

    The refusal is here, and not in the caller, because the stamped body would otherwise pass along
    with nothing to flag it: `resolver_publicado` chooses between manifest and index by
    `"caminho" in ponteiro`, and a body with neither falls into the index branch and comes out as
    `lake_publicacao_vazia` - a generic code ("no parquet for the scope") for a specific cause with
    a known fix.
    """
    if not isinstance(corpo, dict):
        return
    marca = corpo.get(MARCA_OBSOLETO)
    if not marca:
        return
    detalhe = marca if isinstance(marca, dict) else {}
    chaves = detalhe.get("chaves") or []
    desde = detalhe.get("desde")
    agora = f" The current layout is {'/'.join(chaves)}." if chaves else ""
    quando = f" Stamped at {desde}." if desde else ""
    raise ErroAplicacao(
        codigo="lake_escopo_obsoleto",
        mensagem=(
            f"Dataset '{dataset}' in layer '{camada}' changed partitioning, and this pointer "
            f"belongs to the previous layout - the data it points at stopped on the migration "
            f"day.{agora}{quando} Update the source `particao` to the current layout."
        ),
        status=400,
    )


def _cache_publicado(camada: str, caminho_rel: str) -> Path:
    """Cache of the published artifact, mirroring the object path. The layer is immutable
    (`execucao_id=<uuid>` in the path), so the cache never needs invalidating."""
    raiz = Path(get_settings().lake_cache_dir) / camada
    destino = (raiz / caminho_rel).resolve()
    if not destino.is_relative_to(raiz.resolve()):
        raise ErroAplicacao(
            codigo="caminho_publicado_invalido",
            mensagem="The published dataset path escapes the cache root.",
            status=400,
        )
    return destino


async def _na_ordem_do_dataset(
    lake: Armazenamento,
    dataset: str,
    camada: str,
    particao: dict[str, str],
    *,
    ambiente: str | None = None,
) -> dict[str, str]:
    """The partition in the order the dataset was PUBLISHED in, not that of the dictionary that
    arrived.

    The hive path is positional (`unit_id=188/competencia=2026-07-01`), but the `particao` of the
    `fonte` node is a JSON object - and the pipeline definition lives in a `jsonb` column, which
    **does not preserve key order**: Postgres returns the keys sorted by (length, bytes). A
    definition written as `{unit_id, competencia}` comes back as `{competencia, unit_id}` and the
    prefix built points to a directory that does not exist.

    It never showed up while every published dataset had ONE partition key, since with one key
    there is no order to lose. With two, the read returns 404 - loud, rather than wrong data, which
    is the only piece of luck in this story.

    The authoritative order is that of the dataset INDEX (`<dataset>/_atual.json`, field `chaves`),
    which `publicar_indice` writes from the ORDERED list declared in the `saida` node. It is only
    worth consulting with two or more keys; and if the index is not there (dataset not yet
    published, or published without partitioning), what came in is returned - the next error is the
    usual one.
    """
    ambiente, fisica = separar_ambiente_particao(particao, ambiente=ambiente)
    if len(fisica) < 2:
        return fisica
    # The index is only downloaded to read `chaves`, which changes only when the dataset switches
    # layout (partition migration) - and it grows one scope per (unit x as-of) on every load:
    # downloading and decoding O(E) on every `publicado` source with two keys just to read a fixed
    # list. Per-process memo with a TTL, which bounds how long a layout switch takes to be seen
    # without restarting the worker.
    memo = (lake.raiz_uri(camada), camada, ambiente, dataset)
    guardado = _CHAVES_DO_DATASET.get(memo)
    agora = time.monotonic()
    if guardado is not None and agora - guardado[0] < _TTL_CHAVES_S:
        todas = guardado[1]
    else:
        try:
            indice = json.loads(
                await lake.ler(camada, f"{prefixo_dataset(dataset, ambiente)}/{PONTEIRO}")
            )
        except ErroAplicacao:
            return particao
        todas = list(indice.get("chaves") or [])
        if todas:
            if len(_CHAVES_DO_DATASET) >= _TETO_CHAVES:
                _CHAVES_DO_DATASET.clear()
            _CHAVES_DO_DATASET[memo] = (agora, todas)
    chaves = [c for c in todas if c in fisica]
    if len(chaves) != len(fisica):
        return particao
    return {c: fisica[c] for c in chaves}


#: `chaves` of each dataset index, per (lake root, layer, environment, dataset). See
#: `_na_ordem_do_dataset`.
_CHAVES_DO_DATASET: dict[tuple, tuple[float, list[str]]] = {}
_TTL_CHAVES_S = 300.0
_TETO_CHAVES = 4096


async def _escopo_opcional_vazio(
    cfg: dict,
    lake,
    dataset: str,
    camada: str,
    particao: dict[str, str],
    erro: ErroAplicacao,
    *,
    ambiente: str | None = None,
) -> dict | None:
    """SYNTHETIC pointer for a missing scope when the source is `opcional: true` - or None, in which
    case the caller raises the usual error.

    It exists for the period that was NEVER closed. A report cover that reads the summary of
    months m-1 and m-2 already discards, in its SQL, any competence not declared as locked - but the
    source used to fail BEFORE that, with `lake_dataset_ausente`, and took down the whole chain.
    The month before the first one ever closed exists nowhere, so no seeding would bring it.

    **The schema comes from the dataset INDEX**, which is why the tolerance does not apply to a
    dataset that was never published: without a previous publication there are no columns, and a
    table with no schema would make the next node fail with "column does not exist" - trading a
    named error for a confusing one. Only `lake_dataset_ausente` (a 404 on the pointer) comes through
    here; any other failure keeps propagating.

    **The exception is a source that DECLARES its columns** (`config.colunas`): then the schema does
    not depend on the dataset existing, and a never-published dataset also becomes an empty table.
    This is the case of the previous month in a lake where the dataset has not published anything
    yet - without it, the first run of each lake required a seed, and without the seed the whole
    chain failed. The index, when it exists, still wins: it says which columns the dataset actually
    has.
    """
    if not cfg.get("opcional") or erro.codigo != "lake_dataset_ausente":
        return None
    declaradas = cfg.get("colunas") if isinstance(cfg.get("colunas"), list) else []
    try:
        indice = json.loads(
            await lake.ler(camada, f"{prefixo_dataset(dataset, ambiente)}/{PONTEIRO}")
        )
    except ErroAplicacao as e:
        if e.codigo != "objeto_ausente" or not declaradas:
            return None
        indice = {}
    colunas = indice.get("colunas") or declaradas
    if not colunas:
        return None
    return {
        "dataset": dataset,
        "camada": camada,
        "colunas": list(colunas),
        "particao": dict(particao or {}),
        "escopo_ausente": True,
        # No `execucao_id`: there was no publication for this scope, and inventing a provenance
        # would be worse than having none - the lineage states plainly that nothing was read.
        "execucao_id": None,
        "momento": None,
    }


async def resolver_publicado(
    cfg: dict, *, parametros: dict | None = None
) -> tuple[dict, list[str]]:
    """Dataset PUBLISHED by a `saida` node (layer `silver`/`gold`) -> `(pointer, paths)`.

    It reads the artifact, not the pipeline that produced it: whoever consumes a published dataset
    reads the parquet that the load published, and does not make the load run again. Always the
    CURRENT publication of the scope - `<scope>/_atual.json` is moved on every publication, so
    several loads on the same day converge on the last one, which is what is wanted.

    **Through the pointer, never by glob.** `execucao_id=<uuid>` sits in the MIDDLE of the path: a
    `read_parquet('<dataset>/**/dados.parquet')` would bring ALL the runs and sum the dataset once
    per load - silently, with a plausible total. It is the trap the partition design is meant to
    avoid.

    The scope comes from `config.particao` (`{"unit_id": ":unit_id"}` resolves the run parameter).
    Without a partition, it reads the pointer of the whole dataset - which in a dataset published
    PER UNIT would describe the last unit to finish, whoever it was.
    """
    dataset = cfg.get("dataset")
    camada = cfg.get("camada") or "gold"
    ambiente = ambiente_da_camada(camada, parametros, obrigatorio_gold=camada == "gold")
    lake = await abrir(cfg.get("conexao_id"))
    particao = {
        chave: texto_particao(_valor(bruto, parametros))
        for chave, bruto in (cfg.get("particao") or {}).items()
    }
    if camada == "gold":
        ambiente, particao = separar_ambiente_particao(particao, ambiente=ambiente)
    else:
        particao = normalizar_particao(particao) if particao else {}
    particao = await _na_ordem_do_dataset(lake, str(dataset), camada, particao, ambiente=ambiente)
    try:
        ponteiro = await ler_ponteiro(
            lake, str(dataset), camada, particao or None, ambiente=ambiente
        )
    except ErroAplicacao as e:
        vazio = await _escopo_opcional_vazio(
            cfg,
            lake,
            str(dataset),
            camada,
            particao,
            e,
            ambiente=ambiente,
        )
        if vazio is None:
            raise
        return vazio, []

    validar_documento_publicado(
        ponteiro,
        dataset=str(dataset),
        camada=camada,
        ambiente=ambiente,
        particao_esperada=particao if "caminho" in ponteiro else None,
    )

    if "caminho" in ponteiro:  # manifest of ONE scope
        relativos = [ponteiro["caminho"]]
    else:  # run index: one path per published scope
        relativos = [p["caminho"] for p in ponteiro.get("particoes", [])]
    # The pointer stores the path INSIDE the layer; DuckDB needs a file it can reach. `materializar`
    # downloads from the bucket into the local cache (and is a no-op on the local backend) - without
    # it `read_parquet` receives "dataset/unit_id=26/.../dados.parquet" and finds nothing.
    caminhos = [
        await lake.materializar(camada, rel, _cache_publicado(camada, rel)) for rel in relativos
    ]
    if not caminhos:
        raise ErroAplicacao(
            codigo="lake_publicacao_vazia",
            mensagem=(
                f"Dataset '{dataset}' in layer '{camada}' has no published parquet for the "
                "requested scope."
            ),
            status=400,
        )
    return ponteiro, caminhos


def _definicao(bruta) -> dict:
    return (json.loads(bruta) if isinstance(bruta, str) else bruta) or {}


#: `publicacoes_lake(definition)` per version signature, per process (see `produtor_na_cadeia`).
#: The cap only keeps the dictionary from growing without limit in a long-lived worker.
_PUBLICACOES_POR_DEFINICAO: dict[str, frozenset] = {}
_TETO_PUBLICACOES = 1024
#: Resolution of the `publicado` source INSIDE the chain, per (producer, dataset, layer, requested
#: scope). Immutable by construction: the DAG order guarantees the producer has already finished
#: when the consumer runs, and what is read from it is the publication OF THAT run - a path with
#: `execucao_id=` and an index written once. Without the memo, the same few datasets are resolved
#: dozens of times per run: dozens of query pairs and index reads for a handful of answers.
_PUBLICADO_NA_CADEIA: dict[tuple, tuple | None] = {}
_TETO_PUBLICADO = 4096
#: Signature of a `pipeline_versoes` row (`{v}` = table prefix in the query): published ->
#: `v:<pipeline>:<number>` (immutable where it matters), draft -> `m:<md5 of the content>`.
_SQL_ASSINATURA = (
    "CASE WHEN {v}status = 'publicada' "
    "THEN 'v:' || {v}pipeline_id::text || ':' || {v}numero::text "
    "ELSE 'm:' || md5({v}definicao::text) END"
)


async def produtor_na_cadeia(
    cfg: dict, fontes_execucao: dict | None, *, excluir=None
) -> str | None:
    """The run IN THIS chain that publishes the `(dataset, layer)` read by the `publicado` source -
    or None, in which case the read is the usual one (the pointer, `resolver_publicado`).

    `fontes_execucao` is the chain freshness map ({pipeline_id -> execucao_id}); empty means
    outside a chain. The producer is recognised by the definition of the VERSION IT EXECUTED, by the
    same rule as the implicit order (`dag.publicacoes_lake`): today's version may publish something
    else, and a rule written twice would diverge from the one that ordered the chain.

    A producer still `executando` does not count. In the workflow engine the whole chain is
    pre-created, and a producer that has not finished when the reader runs is one that runs AFTER
    it - the implicit edge that would close a cycle and that `dag.py` discarded; inline, it would
    not even be in the map. In both paths the read is the previous one. Once finished (completed or
    failed) it counts, and if it did not publish that is a named error: falling back to the pointer
    would read a run of another as-of, which is the defect.

    `excluir` is the reader's own run: whoever reads and publishes the same dataset does not wait for
    the publication it is still going to make itself."""
    ids = sorted({str(e) for e in (fontes_execucao or {}).values()} - {str(excluir)})
    if not ids:
        return None
    from ..pipelines.dag import publicacoes_lake  # late: `dag` pulls in the pipelines service

    lido = (str(cfg.get("dataset")), str(cfg.get("camada") or "gold"))
    # The whole definition of EACH run in the chain used to come into Python and be decoded on the
    # event loop for every `publicado` source (up to dozens of jsonb values), for a membership test.
    # Now only a SIGNATURE comes back and `publicacoes_lake` is cached by it; the definition only
    # crosses when the signature is new. PUBLISHED version: the signature is (pipeline, number),
    # without reading the definition - the published row only changes through `atualizar_layout`,
    # which touches the node POSITIONS, and `publicacoes_lake` does not read positions. DRAFT (the
    # root of a Studio run): md5 of the content, because it changes without changing number.
    async with transacao() as conn:
        linhas = (
            (
                await conn.execute(
                    text(
                        "SELECT e.id::text AS id, e.status, pl.nome, e.pipeline_id, "
                        f"e.versao_numero, {_SQL_ASSINATURA.format(v='v.')} AS assinatura "
                        "FROM execucoes e "
                        "JOIN pipeline_versoes v "
                        "  ON v.pipeline_id = e.pipeline_id AND v.numero = e.versao_numero "
                        "JOIN pipelines pl ON pl.id = e.pipeline_id "
                        # `e.id = ANY(uuid[])`, not `e.id::text = ANY(text[])`: the cast on the
                        # column defeated the PK index and scanned all of `execucoes` per source.
                        "WHERE e.id = ANY(:ids)"
                    ),
                    {"ids": [uuid.UUID(i) for i in ids]},
                )
            )
            .mappings()
            .all()
        )
        faltam = {
            (p["pipeline_id"], p["versao_numero"])
            for p in linhas
            if p["assinatura"] not in _PUBLICACOES_POR_DEFINICAO
        }
        # Per (pipeline, number) IN THIS call: a draft edited between two reads would have another
        # signature, and what counts is the content that was just read.
        lidas: dict[tuple, set] = {}
        if faltam:
            pares = sorted(faltam, key=str)
            for d in (
                (
                    await conn.execute(
                        text(
                            "SELECT pipeline_id, numero, "
                            f"{_SQL_ASSINATURA.format(v='')} AS assinatura, "
                            "definicao FROM pipeline_versoes "
                            "WHERE (pipeline_id, numero) IN ("
                            "  SELECT * FROM unnest(CAST(:p AS uuid[]), CAST(:n AS int[])))"
                        ),
                        {"p": [p for p, _ in pares], "n": [n for _, n in pares]},
                    )
                )
                .mappings()
                .all()
            ):
                pub = lidas[(d["pipeline_id"], d["numero"])] = publicacoes_lake(
                    _definicao(d["definicao"])
                )
                if len(_PUBLICACOES_POR_DEFINICAO) >= _TETO_PUBLICACOES:
                    _PUBLICACOES_POR_DEFINICAO.clear()
                _PUBLICACOES_POR_DEFINICAO[d["assinatura"]] = frozenset(pub)
    produtores = [
        p
        for p in linhas
        if lido
        in (
            _PUBLICACOES_POR_DEFINICAO.get(p["assinatura"])
            or lidas.get((p["pipeline_id"], p["versao_numero"]))
            or ()
        )
    ]
    if len(produtores) > 1:
        # Two pipelines of the same run publishing the same dataset compete for the pointer;
        # silently picking one would read the half the other did not cover. Loud, naming both.
        nomes = ", ".join(f"'{p['nome']}' ({p['id']})" for p in produtores)
        raise ErroAplicacao(
            codigo="publicado_da_cadeia_ambiguo",
            mensagem=(
                f"Dataset '{lido[0]}' (layer '{lido[1]}') is published by more than one pipeline "
                f"in this chain: {nomes}. There is no way to tell which publication the source "
                "reads."
            ),
            status=409,
        )
    if not produtores or produtores[0]["status"] == "executando":
        return None
    return produtores[0]["id"]


def _publicado_da_cadeia_ausente(dataset: str, camada: str, execucao_id, detalhe: str):
    return ErroAplicacao(
        codigo="publicado_da_cadeia_ausente",
        mensagem=(
            f"Dataset '{dataset}' (layer '{camada}') is produced in this chain by run "
            f"{execucao_id}, which did not publish it for the scope read. {detalhe} Reading the "
            "current pointer would bring the publication of another run, with another as-of - and "
            "the closing would come out green anyway."
        ),
        status=409,
    )


def _escopo_pedido(cfg: dict, parametros: dict | None) -> dict[str, str]:
    """The partition the source asks for, with ITS parameters - the same reading as
    `resolver_publicado`, so the two resolutions never disagree about the scope."""
    return {
        chave: texto_particao(_valor(bruto, parametros))
        for chave, bruto in (cfg.get("particao") or {}).items()
    }


async def _saida_da_execucao(cfg: dict, execucao_id) -> tuple[dict, dict]:
    """(run, config of the `saida` node that publishes the source dataset/layer), by the VERSION the
    run executed - steps 1 and 2 of `ingestao/publicados.py`."""
    from . import publicados  # late: `publicados` imports this module

    dataset = str(cfg.get("dataset"))
    camada = str(cfg.get("camada") or "gold")
    async with transacao() as conn:
        execucao = await publicados.carregar_execucao(conn, execucao_id)
        cfg_saida = await publicados.saida_publicada(conn, execucao, camada, dataset)
    return execucao, cfg_saida


async def resolver_publicado_na_cadeia(
    cfg: dict,
    fontes_execucao: dict | None,
    *,
    excluir=None,
    parametros: dict | None = None,
) -> tuple[dict, list[str]] | None:
    """The `publicado` source inside a chain: `(document, paths)` of the publication made IN THIS
    run, or None - in which case the caller reads the pointer, as usual.

    None in two cases: no pipeline in the chain publishes the dataset (`produtor_na_cadeia`), or the
    producer publishes ANOTHER scope. The second is not a detail: a report cover may read a summary
    dataset that another pipeline of the same chain publishes, but for the previous months - the
    FROZEN history, which by definition belongs to another run. This run produces the requested
    scope when every PARAMETER partition key of the producer output (what is constant in the run,
    computed by the SAME engine function) is requested by the source with the same value. A source
    that does not ask for one of those keys wants more than this run produces: it is also the
    pointer.

    The decision comes from the executed definition and the run parameters, not from the index: that
    is why it holds equally when the producer did NOT publish - and then the scope was this run's,
    which makes the absence an error (`resolver_publicado_da_execucao`), never a read of the
    pointer."""
    from ..execucao import engine  # late: the engine imports this module
    from . import publicados

    produtor = await produtor_na_cadeia(cfg, fontes_execucao, excluir=excluir)
    if produtor is None:
        return None
    pedido = _escopo_pedido(cfg, parametros)
    chave_memo = (
        str(produtor),
        str(cfg.get("dataset")),
        str(cfg.get("camada") or "gold"),
        tuple(sorted(pedido.items())),
    )
    if chave_memo in _PUBLICADO_NA_CADEIA:
        return _PUBLICADO_NA_CADEIA[chave_memo]
    execucao, cfg_saida = await _saida_da_execucao(cfg, produtor)
    base = engine._particao_de_parametro(
        list(cfg_saida.get("particao") or []),
        execucao["parametros"],
        tipos_parametro=execucao.get("tipos_parametro"),
    )
    if not all(c in pedido and publicados._casa(v, pedido[c]) for c, v in base.items()):
        _memorizar_publicado(chave_memo, None)
        return None
    # For a COLUMN key the definition does not decide: the value the run produces is in the DATA.
    # This is the real case of a summary dataset partitioned by `competencia` per column, read by a
    # cover that asks for the two previous months. A value the run did not publish, HAVING published
    # others, is another scope: the pointer (the frozen history). With no published scope at all, it
    # stays a named error.
    por_coluna = {
        str(p.get("chave") or p.get("nome") or "").lower()
        for p in cfg_saida.get("particao") or []
        if isinstance(p, dict) and p.get("origem") == "coluna"
    }
    resolvido = await _ler_da_execucao(
        cfg, execucao, cfg_saida, pedido, coluna_fora_vira_ponteiro=bool(por_coluna & set(pedido))
    )
    _memorizar_publicado(chave_memo, resolvido)
    return resolvido


def _memorizar_publicado(chave: tuple, valor) -> None:
    """Store only RESULTS. A named error (missing dataset) is left out on purpose: it describes the
    lake state, which changes, and not the publication, which does not."""
    if len(_PUBLICADO_NA_CADEIA) >= _TETO_PUBLICADO:
        _PUBLICADO_NA_CADEIA.clear()
    _PUBLICADO_NA_CADEIA[chave] = valor


async def resolver_publicado_da_execucao(
    cfg: dict, *, execucao_id, parametros: dict | None = None
) -> tuple[dict, list[str]]:
    """The source `(dataset, layer)` as run `execucao_id` PUBLISHED it -> `(document, paths)` - the
    same pair as `resolver_publicado`, without going through the pointer.

    The resolution is that of `ingestao/publicados.py` (run -> executed version -> `saida` node ->
    IMMUTABLE index or manifest of the run -> exact list of parquets); rewriting it here would create
    a second truth about where the index lives. The scope is the SOURCE own (`config.particao` with
    its parameters, as with the pointer), checked against the index by `selecionar_particoes`: a
    parameter key must match the run one, a column key filters the scopes.

    Everything that means "this run did not publish that" - missing index (upstream not published,
    failure before the output), a scope that does not match, a parquet that disappeared - comes out
    as `publicado_da_cadeia_ausente`, never as a read of the pointer."""
    execucao, cfg_saida = await _saida_da_execucao(cfg, execucao_id)
    return await _ler_da_execucao(cfg, execucao, cfg_saida, _escopo_pedido(cfg, parametros))


async def _ler_da_execucao(
    cfg: dict,
    execucao: dict,
    cfg_saida: dict,
    pedido: dict[str, str],
    *,
    coluna_fora_vira_ponteiro: bool = False,
) -> tuple[dict, list[str]] | None:
    """`(document, paths)` of what the run published for the request. With
    `coluna_fora_vira_ponteiro`, None when it published scopes and none matches the request - the
    scope belongs to another run, and the caller reads the pointer."""
    from . import publicados

    dataset = str(cfg.get("dataset"))
    camada = str(cfg.get("camada") or "gold")
    execucao_id = execucao["id"]
    try:
        pub = await publicados.abrir_publicacao(execucao, cfg_saida, camada, dataset)
        selecionadas = publicados.selecionar_particoes(
            pub.particoes,
            pedido,
            base=pub.base,
            chaves_coluna=pub.chaves_coluna,
            parametros=pub.parametros,
            dataset=dataset,
            execucao_id=pub.execucao_id,
            tipos_parametro=pub.tipos_parametro,
        )
        if not selecionadas and coluna_fora_vira_ponteiro and pub.particoes:
            return None
        caminhos = await publicados._materializar(pub, selecionadas)
    except ErroAplicacao as e:
        if e.codigo != "publicado_ausente":
            raise
        raise _publicado_da_cadeia_ausente(dataset, camada, execucao_id, e.mensagem) from None
    if not caminhos:
        escopo = ", ".join(f"{c}={v}" for c, v in pedido.items()) or "no partition"
        raise _publicado_da_cadeia_ausente(
            dataset,
            camada,
            execucao_id,
            f"No scope it published matches the request ({escopo}).",
        )
    return pub.documento, caminhos
