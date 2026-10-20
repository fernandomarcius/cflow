"""Connector protocol and common utilities (safe value serialisation)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Protocol

from ...errors import ErroAplicacao


@dataclass
class ResultadoTeste:
    ok: bool
    causa: str | None = None

    def dict(self) -> dict:
        d: dict = {"ok": self.ok}
        if self.causa is not None:
            d["causa"] = self.causa
        return d


def serial(v):
    """Converts driver values to JSON-safe types (dates, decimals, uuid, bytes)."""
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    return v


class Conector(Protocol):
    """Contract of a source connector. Implementations resolve the secret only at runtime and
    never echo the secret or the full connection string in errors."""

    async def testar(self, config: dict, senha: str | None) -> ResultadoTeste: ...

    async def schemas_tabelas(self, config: dict, senha: str | None) -> list[dict]: ...

    async def colunas(
        self, config: dict, senha: str | None, schema: str, tabela: str
    ) -> list[dict]: ...

    async def amostra(
        self, config: dict, senha: str | None, sql: str, limite: int, params: dict | None = None
    ) -> tuple[list[str], list[dict]]:
        """Returns `(colunas, linhas)`. Columns come from the result schema and exist EVEN with
        zero rows, so the downstream DuckDB table has the real columns (nothing breaks)."""
        ...


class ConectorEscrita(Protocol):
    """Contract of a connector that WRITES to the destination (`escrita_sql` effect node).

    ATOMIC write in a single transaction: rows go to a temporary staging table and the final
    statement (per strategy) runs against the target; error -> rollback -> no partial write. The
    secret is resolved only at runtime and never shows up in errors (the cause is sanitised).
    Returns affected rows."""

    async def escrever_tabela(
        self,
        config: dict,
        senha: str | None,
        *,
        schema: str,
        tabela: str,
        colunas_destino: list[str],
        linhas: list[dict],
        estrategia: str,
        chave: list[str] | None,
        lote_tamanho: int,
    ) -> int:
        """Writes `linhas` (dicts keyed by the DESTINATION columns) to `schema.tabela`.
        `estrategia` is one of append | upsert | truncate_insert; `chave` is required for upsert."""
        ...

    async def escrever_tabela_no_escopo(
        self,
        config: dict,
        senha: str | None,
        *,
        schema: str,
        tabela: str,
        colunas_destino: list[str],
        linhas: list[dict],
        estrategia: str,
        chave: list[str] | None,
        lote_tamanho: int,
        escopo: dict[str, str],
        params: dict,
    ) -> dict:
        """Like `escrever_tabela`, but REPLACES the scope (`limpar_escopo`): deletes the rows of
        `escopo` ({coluna: ':parametro'}, value bound from `params`) and writes, in the SAME
        transaction, serialised against another run of the same scope. Refuses (without touching
        the destination) a parameter with no value and an input row outside the scope.
        Returns `{"linhas_afetadas": n, "linhas_apagadas": m}`."""
        ...

    async def executar_dml(
        self,
        config: dict,
        senha: str | None,
        sql: str,
        params: dict | None,
        entrada: tuple[list[str], list[dict]] | None,
    ) -> int:
        """Runs ONE free-form DML statement. Pipeline `:params` go through native binding. `entrada`
        (`(colunas, linhas)`), when present, becomes the `_entrada` staging table (TEXT columns; the
        user SQL does the CAST). Returns affected rows."""
        ...


# --- SCOPED write (`limpar_escopo`) -----------------------------------------------------------
# Clearing the scope in one node and writing in another are two transactions: re-running "from" the
# write skips the clearing (it stays upstream, reused) and two runs of the same scope interleave
# DELETE and MERGE, both leaving rows from the previous run adding up with the new ones. That is why
# the DELETE lives INSIDE the write. The rules below are the same in both connectors and in plan
# assembly (preview), so they live in one place.

REF_PARAMETRO_ESCOPO = re.compile(r"^:([A-Za-z_][A-Za-z0-9_]*)$")
ESTRATEGIAS_ESCOPO = ("append", "upsert")


def parametro_do_escopo(ref) -> str:
    """`':nome'` -> `'nome'`. Only this goes into the TEXT of the clearing SQL; the value is bound."""
    m = REF_PARAMETRO_ESCOPO.match(ref) if isinstance(ref, str) else None
    if not m:
        raise ErroAplicacao(
            codigo="escrita_escopo_invalido",
            mensagem=f"limpar_escopo: {ref!r} is not a parameter (use ':nome').",
            status=400,
        )
    return m.group(1)


def conferir_escopo(escopo, estrategia: str, colunas_destino: list[str]) -> None:
    """Validates the scope shape before any SQL: non-empty (a DELETE without WHERE would wipe the
    table), a compatible strategy (`truncate_insert` already deletes EVERYTHING; adding a scope would
    be contradictory) and every scope column WRITTEN, otherwise rows land without the scope value and
    the next run's DELETE never reaches them."""
    if not isinstance(escopo, dict) or not escopo:
        raise ErroAplicacao(
            codigo="escrita_escopo_invalido",
            mensagem="limpar_escopo must be a non-empty object {column: ':parameter'}.",
            status=400,
        )
    if estrategia not in ESTRATEGIAS_ESCOPO:
        raise ErroAplicacao(
            codigo="escrita_escopo_estrategia",
            mensagem=(
                f"limpar_escopo is incompatible with strategy '{estrategia}' "
                f"(use {' or '.join(ESTRATEGIAS_ESCOPO)})."
            ),
            status=400,
        )
    for ref in escopo.values():
        parametro_do_escopo(ref)
    fora = [c for c in escopo if c not in colunas_destino]
    if fora:
        raise ErroAplicacao(
            codigo="escrita_escopo_fora_das_colunas",
            mensagem=(
                "limpar_escopo columns that the write does not store: "
                + ", ".join(fora)
                + ". The row would land without the scope value and no run would delete it."
            ),
            status=400,
        )


def valores_do_escopo(escopo: dict[str, str], params: dict | None) -> dict:
    """`{parameter: value}` for the scope binding. A parameter with no value (missing, `None` or
    blank) is a hard refusal: `WHERE col = NULL` deletes nothing and the write lands on top of the
    previous run, the same defect, now silent."""
    if not isinstance(escopo, dict) or not escopo:
        raise ErroAplicacao(
            codigo="escrita_escopo_invalido",
            mensagem="limpar_escopo must be a non-empty object {column: ':parameter'}.",
            status=400,
        )
    valores: dict = {}
    faltando: list[str] = []
    for ref in escopo.values():
        nome = parametro_do_escopo(ref)
        v = (params or {}).get(nome)
        if v is None or (isinstance(v, str) and not v.strip()):
            if nome not in faltando:
                faltando.append(nome)
            continue
        valores[nome] = v
    if faltando:
        raise ErroAplicacao(
            codigo="escrita_escopo_sem_valor",
            mensagem=(
                "Write scope has no value for: "
                + ", ".join(faltando)
                + ". Without it, clearing the scope would delete nothing and the write would land on top."
            ),
            status=400,
        )
    return valores


# --- E-mail (`email` effect node) --------------------------------------------------------------
# The message is an immutable VALUE: whoever builds it (`execucao.no_email`) knows no SMTP, and
# whoever sends it (`ConectorEmail`) knows nothing about pipelines/DuckDB. This split lets the
# dry-run build exactly the same message the execution sends, without opening a socket.


@dataclass(frozen=True)
class AnexoEmail:
    nome: str
    conteudo: bytes
    tipo_mime: str = "application/octet-stream"

    @property
    def bytes(self) -> int:
        return len(self.conteudo)


@dataclass(frozen=True)
class MensagemEmail:
    para: tuple[str, ...]
    assunto: str
    corpo: str
    corpo_html: bool = True
    cc: tuple[str, ...] = ()
    cco: tuple[str, ...] = ()
    responder_para: str | None = None
    remetente_nome: str | None = None
    anexos: tuple[AnexoEmail, ...] = field(default_factory=tuple)

    @property
    def destinatarios(self) -> str:
        """Visible to/cc, for the summary and audit trail. `cco` stays OUT: it is a blind copy, and
        exposing it in the snapshot (read by the UI and downstream) defeats its purpose."""
        return ", ".join((*self.para, *self.cc))

    @property
    def bytes_anexos(self) -> int:
        return sum(a.bytes for a in self.anexos)


class ConectorEmail(Protocol):
    """Contract of a connector that SENDS e-mail (`email` effect node).

    The secret is resolved only at runtime and never shows up in errors (the cause is sanitised).
    Retrying belongs to the connector and only makes sense for transport failures: a refused
    credential does not improve by repeating. Returns the send summary (accepted/refused
    recipients, attempts used)."""

    async def enviar(
        self,
        config: dict,
        senha: str | None,
        mensagem: MensagemEmail,
        *,
        timeout: int | None = None,
        tentativas: int | None = None,
    ) -> dict:
        """Sends `mensagem` through the server in `config` (credential already resolved). `timeout`
        and `tentativas` override what the connection configures. Raises `ErroAplicacao` on failure."""
        ...
