"""PII tokenization at WRITE time: the national ID never reaches the published data.

## The gap this closes

Column-level masking in the warehouse (a policy tag with a `SHA256` rule) protects only **one of
the two doors**. The Parquet files in object storage still hold the ID in clear, and a query engine
that reads them by absolute path never goes through the policy tag. Whoever queries the warehouse
sees masked values; whoever reads the file sees everything.

Tokenizing on write closes both at once, because the published data **no longer contains** the ID.

## Why a keyed HMAC and not a hash

An unsalted `SHA-256` over a national ID is **not anonymization**: the space of valid IDs is small
enough to test exhaustively in minutes on a commodity GPU. It is a barrier against casual viewing,
not against someone determined to re-identify. With an HMAC, whoever lacks the key cannot test any
candidate, and the key lives in the KMS behind IAM that only ingestion can reach
(`roles/cloudkms.signerVerifier`). Re-identification stops being "having read access" and becomes
an authorized, logged operation.

## Why this is viable: the DISTINCT-values cache

The KMS API (`MacSign`) is **one call per value**; there is no batch form. Measured on production-like
data, the classified columns hold hundreds of thousands of non-empty cells but only a few thousand
DISTINCT values (a reduction above 95 %). Tokenizing cell by cell would be unworkable (hundreds of
thousands of calls per republication). Tokenizing each **distinct value** and mapping it back takes
a few thousand calls: seconds, and a negligible cost. **The whole viability of this feature rests on
that difference**; do not replace the map with a per-row call.

## What is NOT tokenized, and why

Low-cardinality contact and address columns (e-mail, phone, street, district, postal code) stay
out for now, both for product reasons rather than code reasons: tokenizing a postal code destroys
regional aggregation, and a space of a few hundred distinct values is trivially brute-forceable even
with a key, so an HMAC there protects little and costs analysis. They keep the policy tag.

Person NAMES are not numeric identifiers either and fall in the same bucket.

## Fail LOUD when there is no tokenizer

Without a KMS (local backend, air-gapped), publishing a dataset with a classified column is
**refused**. Writing the ID in clear because the KMS did not answer is precisely the outcome this
module exists to prevent, and it would be silent.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Protocol

import httpx
import pyarrow as pa
import pyarrow.compute as pc

from ..config import get_settings
from ..errors import ErroAplicacao
from ..retry import espera
from .armazenamento import Armazenamento

logger = logging.getLogger("cflow.ingestao.tokenizacao")

# Tax-ID columns per silver dataset. The list is DECLARED, never inferred from the name: a column
# called `document` may not be an ID, and an ID column may carry nothing in its name that gives it
# away (same reasoning as the identifier vetoes in `perfis.py`). Replace with your own datasets.
COLUNAS_PII: dict[str, tuple[str, ...]] = {
    "registry": ("tax_id_owner",),
    "orders_detail": (
        *(f"tab_a_tax_id_party_{i}" for i in range(1, 10)),
        *(f"tab_b_tax_id_party_{i}" for i in range(1, 10)),
    ),
}

# Token prefix. It keeps anyone from mistaking a token for a real ID when looking at the data, and
# makes an accidental double tokenization detectable instead of silent.
PREFIXO = "tk_"
# 128 bits of the HMAC-SHA256, in hex. Truncating does not meaningfully weaken collision resistance
# at this volume (~10^4 distinct values) and keeps the column readable in a grid.
DIGITOS = 32


class Tokenizador(Protocol):
    """Turns a value into a token. Receives DISTINCT values, returns the map."""

    async def mapear(self, valores: list[str]) -> dict[str, str]: ...


class TokenizadorKms:
    """Cloud KMS `MacSign`, one call per DISTINCT value.

    The access token comes from the lake backend, which obtains it from the secret store, the same
    path Iceberg uses. No Google SDK, no `GOOGLE_APPLICATION_CREDENTIALS`, no credential file on
    disk: opening a second credential door would defeat the reason the backend token exists.
    """

    def __init__(
        self, *, chave: str, token: str, timeout: float = 30.0, tentativas: int = 3
    ) -> None:
        self._chave = chave
        self._token = token
        self._timeout = timeout
        self._tentativas = max(1, tentativas)

    async def mapear(self, valores: list[str]) -> dict[str, str]:
        url = f"https://cloudkms.googleapis.com/v1/{self._chave}:macSign"
        cabecalhos = {"Authorization": f"Bearer {self._token}"}
        mapa: dict[str, str] = {}
        async with httpx.AsyncClient(timeout=self._timeout) as cliente:
            for v in valores:
                corpo = {"data": base64.b64encode(v.encode("utf-8")).decode("ascii")}
                mapa[v] = await self._assinar(cliente, url, cabecalhos, corpo)
        return mapa

    async def _assinar(self, cliente, url: str, cabecalhos: dict, corpo: dict) -> str:
        """One signature, with the same backoff as the rest of ingestion.

        A KMS failure is an **INFRA** failure, not a data one: it raises, and the workflow retry
        fixes it. Falling back to "publish untokenized" would trade a visible failure for a silent
        leak, and publishing half-tokenized data is worse than not publishing: some rows would hold
        the ID in clear and others a token, with nothing saying which.
        """
        ultimo: Exception | None = None
        detalhe = "no detail"
        for tentativa in range(self._tentativas):
            try:
                r = await cliente.post(url, json=corpo, headers=cabecalhos)
                r.raise_for_status()
                return PREFIXO + base64.b64decode(r.json()["mac"]).hex()[:DIGITOS]
            except httpx.HTTPStatusError as exc:
                ultimo = exc
                # The response BODY goes into the message: the two real failure modes of this
                # feature (URL pointing at the KEY instead of the VERSION -> 404, and a token with
                # storage scope -> 403 `ACCESS_TOKEN_SCOPE_INSUFFICIENT`) would otherwise be
                # indistinguishable from each other and from a network outage.
                detalhe = f"HTTP {exc.response.status_code}: {exc.response.text[:300]}"
                if exc.response.status_code in (400, 401, 403, 404):
                    break  # configuration errors do not improve by retrying
            except Exception as exc:  # noqa: BLE001 - re-raised below
                ultimo = exc
                detalhe = f"{type(exc).__name__}: {str(exc)[:300]}"
            if tentativa < self._tentativas - 1:
                await asyncio.sleep(espera(tentativa))
        raise ErroAplicacao(
            codigo="pii_tokenizacao_falhou",
            mensagem=f"Could not tokenize the personal data. {detalhe}",
            status=503,
        ) from ultimo


def colunas_de(dataset: str, esquema: pa.Schema) -> list[str]:
    """The classified columns that EXIST in this table.

    Declaring a column that does not exist is not an error: the silver schema changes as typing
    advances, and a list that had to track every change would break publication over a name
    mismatch. What must never happen is the opposite (a classified column present and not
    tokenized), which is guaranteed by `tokenizar_tabela` always being called.
    """
    declaradas = COLUNAS_PII.get(dataset, ())
    existentes = set(esquema.names)
    return [c for c in declaradas if c in existentes]


async def tokenizar_tabela(
    *, dataset: str, tabela: pa.Table, tokenizador: Tokenizador | None
) -> pa.Table:
    """Replace the classified columns with tokens. Returns the table unchanged if there are none.

    No tokenizer and a classified column present -> refuse. This is where a missing KMS stops being
    a degradation and becomes an error: the alternative is publishing the ID in clear with no
    warning.
    """
    colunas = colunas_de(dataset, tabela.schema)
    if not colunas:
        return tabela
    if tokenizador is None:
        raise ErroAplicacao(
            codigo="pii_sem_tokenizador",
            mensagem=(
                f"Dataset '{dataset}' has a personal-data column "
                f"({', '.join(colunas)}) and no tokenizer is configured. "
                "Publication was refused: writing the identifier in clear would be worse than "
                "not publishing. Configure the lake tokenization key."
            ),
            status=409,
        )

    # ONE map for the whole table, over the UNION of distinct values of all columns. Per column it
    # would be N calls for the same ID appearing in `tab_a_..._1` and `..._2`, and the token must be
    # the same in both: that is what allows joining.
    distintos: set[str] = set()
    for c in colunas:
        col = tabela.column(c)
        for v in pc.unique(col.combine_chunks()).to_pylist():
            if v is not None and str(v).strip() != "":
                distintos.add(str(v))

    if not distintos:
        return tabela

    mapa = await tokenizador.mapear(sorted(distintos))
    logger.info(
        "pii_tokenizada",
        extra={
            "extra_campos": {
                "dataset": dataset,
                "colunas": len(colunas),
                "valores_distintos": len(distintos),
            }
        },
    )

    for c in colunas:
        original = tabela.column(c).combine_chunks().to_pylist()
        # NULL stays NULL and empty stays empty: "not provided" and "provided X" are different
        # findings, and turning absence into a token would invent a person.
        trocada = [
            None if v is None or str(v).strip() == "" else mapa.get(str(v), PREFIXO + "?")
            for v in original
        ]
        tabela = tabela.set_column(
            tabela.schema.get_field_index(c), c, pa.array(trocada, type=pa.string())
        )
    return tabela


async def abrir_tokenizador(lake: Armazenamento) -> Tokenizador | None:
    """The tokenizer for this environment, or `None` when there is none.

    `None` does **not** mean "may publish in clear": it is what makes `tokenizar_tabela` refuse when
    the dataset has a classified column. Datasets without PII keep publishing normally, which keeps
    the platform usable air-gapped.

    Requires both: the key configured AND a backend able to issue an access token (GCS only). On
    the local backend there is no KMS to reach, and pretending otherwise would produce a network
    error in the middle of publication instead of a clear refusal.
    """
    chave = get_settings().pii_chave_kms
    if not chave:
        return None
    # `macSign` operates on the key VERSION, not the key: pointing at `.../cryptoKeys/<k>` returns
    # an HTML **404** from the Google frontend, which does not even look like an API error.
    # Refusing here, naming the format, is the difference between a five-second configuration fix
    # and a hunt.
    #
    # Pinning the VERSION is also the right design: rotating changes every person's token, so the
    # change must be deliberate and followed by republication, never inherited from a new version
    # that appeared on its own.
    if "/cryptoKeyVersions/" not in chave:
        raise ErroAplicacao(
            codigo="pii_chave_kms_invalida",
            mensagem="CFLOW_PII_CHAVE_KMS must point to the key VERSION "
            "(.../cryptoKeys/<key>/cryptoKeyVersions/<n>), not to the key, "
            f"because KMS signs by version. Current value: {chave!r}",
            status=500,
        )
    obter = getattr(lake, "token_para_kms", None)
    if obter is None:
        return None
    return TokenizadorKms(chave=chave, token=await obter())
