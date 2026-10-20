"""E-mail template rendering: `{{placeholder}}` substitution. PURE, no I/O.

Plain substitution, not a template engine (Jinja and the like): the body is HTML the author writes
and edits in the editor; an engine would turn that field into an expression-execution surface with
no gain for the use case.

An unrecognised placeholder becomes an EMPTY string and is RETURNED in the unresolved list: silence
here sends an e-mail with a hole in the middle of the text, and the dry-run must be able to warn
beforehand.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..timezone import FUSO_SAO_PAULO, agora_utc, para_exibicao

# `{{ nome }}`, `{{param.competencia}}`: a simple identifier, optionally dotted.
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")

TABELA_LIMITE_PADRAO = 100


def texto_valor(v: object) -> str:
    """Data value as TEXT, without reinterpreting numbers. `Decimal` becomes `str` (every digit
    preserved, same principle as the front-end data grid); `None` becomes empty."""
    if v is None:
        return ""
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, datetime):
        return para_exibicao(v).isoformat()
    return str(v)


@dataclass
class Contexto:
    """Everything a placeholder can resolve. `linha` only exists in `por_linha` mode."""

    colunas: list[str] = field(default_factory=list)
    linhas: list[dict] = field(default_factory=list)
    linha: dict | None = None
    params: dict = field(default_factory=dict)
    html: bool = True
    tabela_limite: int = TABELA_LIMITE_PADRAO
    agora: datetime | None = None

    def data_hora(self) -> str:
        return para_exibicao(self.agora or agora_utc(), FUSO_SAO_PAULO).strftime(
            "%d/%m/%Y %H:%M (%Z)"
        )


def tabela_html(colunas: list[str], linhas: list[dict], limite: int) -> str:
    """HTML table with INLINE style: e-mail clients often drop `<style>` in `<head>`, so border and
    zebra striping go in the attribute, otherwise the table arrives with no grid at all."""
    if not colunas:
        return "<p>(no columns)</p>"
    borda = "border:1px solid #d4d4d4;padding:6px 8px;text-align:left"
    cab = "".join(f'<th style="{borda};background:#eaeaea">{html.escape(c)}</th>' for c in colunas)
    corpo = []
    for i, linha in enumerate(linhas[:limite]):
        fundo = ";background:#f6f6f6" if i % 2 else ""
        tds = "".join(
            f'<td style="{borda}{fundo}">{html.escape(texto_valor(linha.get(c)))}</td>'
            for c in colunas
        )
        corpo.append(f"<tr>{tds}</tr>")
    restante = max(0, len(linhas) - limite)
    rodape = (
        f'<p style="font-size:0.9em;color:#555">… and {restante} more row(s) '
        f"(the table shows the first {limite}; the attachment has everything).</p>"
        if restante
        else ""
    )
    return (
        '<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:0.95em">'
        f"<thead><tr>{cab}</tr></thead><tbody>{''.join(corpo)}</tbody></table>{rodape}"
    )


def tabela_texto(colunas: list[str], linhas: list[dict], limite: int) -> str:
    """Same table as plain text (corpo_tipo='texto'), TAB-separated."""
    if not colunas:
        return "(no columns)"
    saida = ["\t".join(colunas)]
    saida += ["\t".join(texto_valor(linha.get(c)) for c in colunas) for linha in linhas[:limite]]
    restante = max(0, len(linhas) - limite)
    if restante:
        saida.append(f"… and {restante} more row(s) (the attachment has everything).")
    return "\n".join(saida)


def _resolver(nome: str, ctx: Contexto) -> str | None:
    """RAW value of a placeholder, or None if it does not resolve (the caller records it and uses empty)."""
    if nome == "contagem":
        return str(len(ctx.linhas))
    if nome == "data_hora":
        return ctx.data_hora()
    if nome == "tabela":
        return (
            tabela_html(ctx.colunas, ctx.linhas, ctx.tabela_limite)
            if ctx.html
            else tabela_texto(ctx.colunas, ctx.linhas, ctx.tabela_limite)
        )
    if nome.startswith("param."):
        chave = nome[len("param.") :]
        return texto_valor(ctx.params.get(chave)) if chave in ctx.params else None
    if ctx.linha is not None and nome in ctx.linha:
        return texto_valor(ctx.linha[nome])
    return None


def renderizar(texto: str, ctx: Contexto) -> tuple[str, list[str]]:
    """Return `(rendered text, unresolved placeholders)`.

    DATA values are escaped when the body is HTML (a field with `<` or `&` would break the e-mail
    layout, and a `<script>` coming from the database must not become markup). `{{tabela}}` is the
    only exception: it IS markup generated here, with the values already escaped one by one."""
    nao_resolvidos: list[str] = []

    def troca(m: re.Match[str]) -> str:
        nome = m.group(1)
        valor = _resolver(nome, ctx)
        if valor is None:
            if nome not in nao_resolvidos:
                nao_resolvidos.append(nome)
            return ""
        if ctx.html and nome != "tabela":
            return html.escape(valor)
        return valor

    return PLACEHOLDER.sub(troca, texto or ""), nao_resolvidos


def placeholders(texto: str) -> list[str]:
    """Placeholder names used in the text (order of appearance, no repeats). Used by validation to
    refuse references to a `segredo` parameter."""
    vistos: list[str] = []
    for m in PLACEHOLDER.finditer(texto or ""):
        if m.group(1) not in vistos:
            vistos.append(m.group(1))
    return vistos
