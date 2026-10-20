"""Declarative contract of an external source: validation, slicing and URL resolution.

A source is DATA, not code: adding a source means registering a contract. This module is the
grammar of that data: whatever does not pass here never becomes an execution.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..errors import DetalheErro, ErroAplicacao

CLASSES = ("http_file", "rest_api", "odata", "portal_download")
CADENCIAS = ("diaria", "semanal", "mensal", "anual", "manual")
TIPOS_JANELA = ("diaria", "mensal", "anual", "periodo")
TIPOS_FORMATO = ("csv", "json", "largura_fixa", "parquet")
COMPRESSOES = (None, "zip", "gzip")

# `csv` and `largura_fixa` are bytes turned into text: the encoding is REQUIRED. A silent default
# (utf-8) does not fail: it corrupts accented characters and the error only shows up in a report,
# months later.
FORMATOS_COM_ENCODING = ("csv", "largura_fixa")

_SLUG = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
# URL/parameter placeholder: {name} or {name:%d/%m/%Y} (date formatting).
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)(?::([^}]+))?\}")

# Keys the slice offers to any URL/parameter pattern. Everything else must come from `variaveis`.
CHAVES_FATIA = ("ano", "ano_curto", "mes", "dia", "ano_mes", "data", "inicio", "fim", "chave")
# The ones that make the request POINT AT A PERIOD. With none of them, the origin publishes a single
# overwritten file and there is no history to fetch (see `depende_da_fatia`).
CHAVES_TEMPORAIS = CHAVES_FATIA

# --- Grain: what ONE ROW of this table is ------------------------------------------------------
# This is a DIFFERENT question from the one `janela` answers ("how the run is sliced"), which is
# why the block is a SIBLING of `janela` and not a field inside it. Mixing the two produces a
# cadence x grain confusion and duplicated rows that no validation would catch.
ESTADOS_GRAO = ("declarado", "desconhecido", "sem_chave")
# Keys injected by READING the bronze (provenance), not by the origin: they are valid in the grain
# even when absent from `schema_esperado`. `competencia` only exists when
# `janela.fatia_e_competencia` is true.
CHAVES_DE_PROVENIENCIA = ("competencia", "ingestion_date")
# Normalised column name (`gerador_silver.normalizar_nome`). Unlike `_SLUG`, which belongs to the
# `dataset` and requires 3 characters: a two-letter column exists and is a legitimate key.
_SLUG_COLUNA = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class Fatia:
    """One unit of ingestion: a month, a year, a 10-year window.

    `chave` is the identity of the slice: it is part of the uniqueness (source, batch key, sha256)
    that makes a re-run a no-op. Change the key and the identity changes, so it is derived from the
    contract, never from "now"."""

    chave: str
    inicio: date
    fim: date


@dataclass(frozen=True)
class Grao:
    """The declared grain of the table the source produces.

    `estado`:
    - `declarado`: the `chaves` identify a row (this is what allows reporting duplicates);
    - `desconhecido`: nobody declared it yet. It is the default for the ABSENCE and is deliberately
      noisy downstream: silently accepting the absence is the bug this field fixes;
    - `sem_chave`: the table really has no key (repeatable event, aggregate without identity),
      declared with a mandatory `motivo`.

    `desempate` resolves a tie WITHIN the declared key: the origin resubmits the same record in a
    later delivery and only one of the two rows must survive. It is a DIFFERENT question from
    `chaves`: here identity is not discovered, it is chosen WHICH copy is valid.
    `desempate_motivo` is mandatory for the same reason `sem_chave` requires `motivo`: dropping a
    row without saying why is the silent drop this whole field exists to prevent."""

    estado: str
    chaves: tuple[str, ...] = ()
    motivo: str | None = None
    desempate: tuple[tuple[str, str], ...] = ()
    desempate_motivo: str | None = None

    @property
    def declarado(self) -> bool:
        return self.estado == "declarado"

    @property
    def desconhecido(self) -> bool:
        return self.estado == "desconhecido"


def _erro(problemas: list[str]) -> None:
    if problemas:
        raise ErroAplicacao(
            codigo="contrato_invalido",
            mensagem="Invalid external source contract.",
            status=422,
            detalhes=[DetalheErro(campo="contrato", mensagem=m) for m in problemas],
        )


def _placeholders(texto: str) -> set[str]:
    return {m.group(1) for m in _PLACEHOLDER.finditer(texto)}


def _checar_placeholders(texto: str, variaveis: dict, onde: str, problemas: list[str]) -> None:
    """An unknown placeholder is an ERROR, not a hole in the URL: a URL built with a literal
    `{serie}` returns 404 every day without saying why."""
    desconhecidos = _placeholders(texto) - set(CHAVES_FATIA) - set(variaveis or {})
    for p in sorted(desconhecidos):
        problemas.append(f"{onde}: unknown placeholder '{{{p}}}' (declare it in 'variaveis').")


def validar(contrato: dict) -> None:
    """Validate the whole contract. 422 with one detail per problem (all at once: fixing a contract
    one error round-trip at a time is what makes registering a source painful)."""
    p: list[str] = []
    if not isinstance(contrato, dict):
        _erro(["The contract must be a JSON object."])
        return

    classe = contrato.get("classe")
    if classe not in CLASSES:
        p.append(f"'classe' must be one of: {', '.join(CLASSES)}.")
    dataset = contrato.get("dataset") or ""
    if not _SLUG.match(str(dataset)):
        p.append(
            "'dataset' must be a slug (lowercase letters, digits and '_', 3 to 64 characters)."
        )
    if contrato.get("cadencia", "manual") not in CADENCIAS:
        p.append(f"'cadencia' must be one of: {', '.join(CADENCIAS)}.")

    _validar_janela(contrato.get("janela"), p)
    acesso = contrato.get("acesso")
    variaveis = (acesso.get("variaveis") or {}) if isinstance(acesso, dict) else {}
    _validar_formato(contrato.get("formato"), p)
    _validar_acesso(classe, acesso, variaveis, p)
    _validar_limites(contrato.get("limites"), p)

    esperado = contrato.get("schema_esperado")
    if esperado is not None and (
        not isinstance(esperado, list) or not all(isinstance(c, str) and c for c in esperado)
    ):
        p.append("'schema_esperado' must be a list of column names.")
    _validar_grao(contrato.get("grao"), contrato, p)
    _erro(p)


def _validar_janela(j, p: list[str]) -> None:
    if not isinstance(j, dict):
        p.append("'janela' is required (an object with 'tipo').")
        return
    tipo = j.get("tipo")
    if tipo not in TIPOS_JANELA:
        p.append(f"'janela.tipo' must be one of: {', '.join(TIPOS_JANELA)}.")
    if j.get("inicio") is not None and piso_data(j.get("inicio")) is None:
        p.append("'janela.inicio' must be YYYY, YYYY-MM or YYYY-MM-DD.")
    passo = j.get("passo_max_dias")
    if tipo == "periodo":
        if not isinstance(passo, int) or passo < 1:
            p.append("'janela.passo_max_dias' is required (integer >= 1) when tipo='periodo'.")
    elif passo is not None and (not isinstance(passo, int) or passo < 1):
        p.append("'janela.passo_max_dias' must be an integer >= 1.")
    atraso = j.get("atraso_dias", 0)
    if not isinstance(atraso, int) or atraso < 0:
        p.append("'janela.atraso_dias' must be an integer >= 0.")
    comp = j.get("fatia_e_competencia", False)
    if not isinstance(comp, bool):
        p.append("'janela.fatia_e_competencia' must be a boolean.")


def fatia_e_competencia(contrato: dict) -> bool:
    """Is the slice key the competence date of this source's data?

    It must be DECLARED, never inferred. A source that publishes one file per business day has the
    slice `2026-02-23`: that date is the grain of the series, and without it the table cannot
    answer "what was the rate on 03/15". A source whose slice is the year `2026` and whose rows
    carry their own date column must not declare it: projecting the slice would label the whole
    year with a single date.

    `cadencia` is no signal: it is the frequency of EXECUTION (monthly for every source), not the
    grain of the series.
    """
    return bool((contrato.get("janela") or {}).get("fatia_e_competencia", False))


def grao(contrato: dict) -> Grao:
    """The grain of this source, with the ABSENCE normalised to `desconhecido`.

    A contract without `grao` is not an invalid contract: making the field mandatory in
    `validar()` would break every `PUT` on already registered sources at once. But it does not mean
    "everything is fine" either: callers receive an explicit `desconhecido`, the state about which
    noise is made. The default is NEVER "the keys are all the columns": that would make any table
    trivially unique and hide exactly the tables whose rows are byte-for-byte identical."""
    g = contrato.get("grao")
    if not isinstance(g, dict):
        return Grao(estado="desconhecido")
    estado = g.get("estado")
    if estado not in ESTADOS_GRAO:
        # A validated contract never gets here; defensive reading does not invent a plausible grain.
        return Grao(estado="desconhecido")
    chaves = tuple(str(c) for c in (g.get("chaves") or []))
    motivo = g.get("motivo")
    desempate, desempate_motivo = _ler_desempate(g.get("desempate"))
    return Grao(
        estado=estado,
        chaves=chaves,
        motivo=str(motivo) if motivo else None,
        desempate=desempate,
        desempate_motivo=desempate_motivo,
    )


def _ler_desempate(d) -> tuple[tuple[tuple[str, str], ...], str | None]:
    """Defensive reading, symmetrical to `grao()`: an invalid shape becomes `()`, never an invented order."""
    if not isinstance(d, dict):
        return (), None
    colunas = d.get("colunas")
    if not isinstance(colunas, list):
        return (), None
    pares = tuple(
        (str(c["coluna"]), str(c.get("ordem", "asc")))
        for c in colunas
        if isinstance(c, dict) and isinstance(c.get("coluna"), str) and c.get("coluna")
    )
    motivo = d.get("motivo")
    return pares, (str(motivo) if motivo else None)


def _validar_grao(g, contrato: dict, p: list[str]) -> None:
    """Absence is accepted (it becomes `desconhecido` on read); whatever is PRESENT must add up."""
    if g is None:
        return
    if not isinstance(g, dict):
        p.append("'grao' must be an object with 'estado'.")
        return

    estado = g.get("estado")
    if estado not in ESTADOS_GRAO:
        p.append(f"'grao.estado' must be one of: {', '.join(ESTADOS_GRAO)}.")
        return

    chaves = g.get("chaves")
    if chaves is not None and not isinstance(chaves, list):
        p.append("'grao.chaves' must be a list of column names.")
        return
    chaves = list(chaves or [])
    motivo = g.get("motivo")
    if motivo is not None and not isinstance(motivo, str):
        p.append("'grao.motivo' must be text.")

    if estado == "declarado":
        if not chaves:
            p.append("'grao.chaves' is required and cannot be empty when estado='declarado'.")
            return
        for c in chaves:
            if not isinstance(c, str) or not _SLUG_COLUNA.match(c):
                p.append(
                    f"'grao.chaves' contains {c!r}: use the already normalised column name "
                    "(lowercase letters, digits and '_')."
                )
        if len(set(chaves)) != len(chaves):
            p.append("'grao.chaves' has a repeated column.")

        # Catch the typo at REGISTRATION time, not months later in a count that does not add up.
        # Local import: `contrato` is the grammar of the data and must not depend on the silver
        # generator (a higher layer); the import cost is paid only by whoever validates.
        from .gerador_silver import normalizar_nome

        esperado = contrato.get("schema_esperado")
        conhecidas: set[str] = set()
        tem_schema = isinstance(esperado, list) and bool(esperado)
        if tem_schema:
            conhecidas = {normalizar_nome(str(c)) for c in esperado if isinstance(c, str)}
            conhecidas |= set(CHAVES_DE_PROVENIENCIA)
            for c in chaves:
                if isinstance(c, str) and c not in conhecidas:
                    p.append(
                        f"'grao.chaves': '{c}' is not in 'schema_esperado' "
                        f"({', '.join(sorted(conhecidas))})."
                    )

        if "competencia" in chaves and not fatia_e_competencia(contrato):
            p.append(
                "'grao.chaves' uses 'competencia', but 'janela.fatia_e_competencia' is not "
                "enabled: the column does not even exist in the bronze read and the key would "
                "point at nothing."
            )

        _validar_desempate(g.get("desempate"), chaves, conhecidas, tem_schema, p)
        return

    if estado == "sem_chave":
        if chaves:
            p.append("'grao.chaves' must be empty when estado='sem_chave'.")
        if not (isinstance(motivo, str) and motivo.strip()):
            p.append(
                "'grao.motivo' is required when estado='sem_chave': saying the table has no "
                "key must cost one sentence, otherwise it becomes the silent default again."
            )
        if g.get("desempate") is not None:
            p.append("'grao.desempate' only applies with estado='declarado'.")
        return

    if chaves:  # desconhecido
        p.append("'grao.chaves' only applies with estado='declarado'.")
    if g.get("desempate") is not None:  # desconhecido
        p.append("'grao.desempate' only applies with estado='declarado'.")


def _validar_desempate(
    d, chaves: list[str], conhecidas: set[str], tem_schema: bool, p: list[str]
) -> None:
    """`grao.desempate` resolves a tie WITHIN the declared key (e.g. the origin resubmits the same
    record in a later delivery). It only makes sense with the key present (tie-breaking without
    identity is not tie-breaking) and never chooses silently: `motivo` is mandatory for the same
    reason `sem_chave` requires one, above."""
    if d is None:
        return
    if not isinstance(d, dict):
        p.append("'grao.desempate' must be an object with 'colunas' and 'motivo'.")
        return

    colunas = d.get("colunas")
    if not isinstance(colunas, list) or not colunas:
        p.append("'grao.desempate.colunas' is required and cannot be empty.")
        colunas = []

    nomes: list[str] = []
    for c in colunas:
        if not isinstance(c, dict):
            p.append("'grao.desempate.colunas' must be a list of {coluna, ordem} objects.")
            continue
        nome = c.get("coluna")
        ordem = c.get("ordem", "asc")
        if not isinstance(nome, str) or not _SLUG_COLUNA.match(nome):
            p.append(
                f"'grao.desempate.colunas' contains an invalid column {nome!r}: use the already "
                "normalised column name (lowercase letters, digits and '_')."
            )
        else:
            nomes.append(nome)
        if ordem not in ("asc", "desc"):
            p.append(f"'grao.desempate.colunas': 'ordem' must be 'asc' or 'desc' (got {ordem!r}).")

    if len(set(nomes)) != len(nomes):
        p.append("'grao.desempate.colunas' has a repeated column.")

    colisao = set(nomes) & set(chaves)
    if colisao:
        p.append(
            "'grao.desempate.colunas' cannot repeat 'grao.chaves' "
            f"({', '.join(sorted(colisao))}): a column that already identifies the row does not break ties."
        )

    if tem_schema:
        for nome in nomes:
            if nome not in conhecidas:
                p.append(
                    f"'grao.desempate.colunas': '{nome}' is not in 'schema_esperado' "
                    f"({', '.join(sorted(conhecidas))})."
                )

    motivo = d.get("motivo")
    if not (isinstance(motivo, str) and motivo.strip()):
        p.append(
            "'grao.desempate.motivo' is required: dropping a row without saying why is the "
            "silent drop the grain exists to prevent."
        )


def _validar_formato(f, p: list[str]) -> None:
    if not isinstance(f, dict):
        p.append("'formato' is required (an object with 'tipo').")
        return
    tipo = f.get("tipo")
    if tipo not in TIPOS_FORMATO:
        p.append(f"'formato.tipo' must be one of: {', '.join(TIPOS_FORMATO)}.")
    if f.get("compressao") not in COMPRESSOES:
        p.append("'formato.compressao' must be 'zip', 'gzip' or absent.")

    enc = f.get("encoding")
    if tipo in FORMATOS_COM_ENCODING:
        if not enc:
            p.append(
                f"'formato.encoding' is required for '{tipo}' "
                "(e.g. 'latin-1', 'cp1252'); without it accented characters are silently corrupted."
            )
        else:
            try:
                codecs.lookup(str(enc))
            except LookupError:
                p.append(f"'formato.encoding' unknown: '{enc}'.")
    elif enc:
        try:
            codecs.lookup(str(enc))
        except LookupError:
            p.append(f"'formato.encoding' unknown: '{enc}'.")

    tolerar = f.get("tolerar_linhas_invalidas")
    if tolerar is not None:
        if not isinstance(tolerar, bool):
            p.append("'formato.tolerar_linhas_invalidas' must be true or false.")
        elif tolerar and tipo != "csv":
            p.append("'formato.tolerar_linhas_invalidas' only applies to 'csv'.")

    if tipo == "largura_fixa":
        campos = f.get("campos")
        if not isinstance(campos, list) or not campos:
            p.append("'formato.campos' is required for 'largura_fixa'.")
        else:
            for i, c in enumerate(campos):
                if not isinstance(c, dict) or not c.get("nome"):
                    p.append(f"'formato.campos[{i}]' needs a 'nome'.")
                    continue
                if not isinstance(c.get("inicio"), int) or c["inicio"] < 1:
                    p.append(f"'formato.campos[{i}].inicio' must be an integer >= 1 (1-based).")
                if not isinstance(c.get("tamanho"), int) or c["tamanho"] < 1:
                    p.append(f"'formato.campos[{i}].tamanho' must be an integer >= 1.")
    if f.get("prefixo_linha") is not None and not (
        isinstance(f["prefixo_linha"], str) and f["prefixo_linha"]
    ):
        p.append("'formato.prefixo_linha' must be a non-empty string.")
    if f.get("arquivo_no_zip"):
        try:
            re.compile(f["arquivo_no_zip"])
        except re.error as e:
            p.append(f"'formato.arquivo_no_zip' is not a valid regular expression: {e}.")


def _validar_acesso(classe, a, variaveis: dict, p: list[str]) -> None:
    if not isinstance(a, dict):
        p.append("'acesso' is required (an object).")
        return
    tipos_ok = all(isinstance(k, str) and isinstance(v, (str, int)) for k, v in variaveis.items())
    if variaveis and not tipos_ok:
        p.append("'acesso.variaveis' must map name to text/number.")

    url = a.get("url_padrao")
    if classe in ("http_file", "rest_api", "odata"):
        if not isinstance(url, str) or not url.lower().startswith("https://"):
            p.append("'acesso.url_padrao' is required and must start with https://.")
        else:
            _checar_placeholders(url, variaveis, "acesso.url_padrao", p)

    if classe == "rest_api":
        for campo in ("param_inicio", "param_fim"):
            if a.get(campo) is not None and not isinstance(a.get(campo), str):
                p.append(f"'acesso.{campo}' must be text.")
        fmt = a.get("formato_data", "%Y-%m-%d")
        if not isinstance(fmt, str) or "%" not in fmt:
            p.append("'acesso.formato_data' must be a strftime format (e.g. '%d/%m/%Y').")

    if classe == "odata":
        top = a.get("top", 1000)
        if not isinstance(top, int) or not 1 <= top <= 10000:
            p.append("'acesso.top' must be an integer between 1 and 10000.")
        if a.get("paginar") is not None and not isinstance(a.get("paginar"), bool):
            p.append("'acesso.paginar' must be true/false.")
        maxp = a.get("max_paginas", 200)
        if not isinstance(maxp, int) or not 1 <= maxp <= 10000:
            p.append("'acesso.max_paginas' must be an integer between 1 and 10000.")
        for k, v in (a.get("params_janela") or {}).items():
            if not isinstance(v, str):
                p.append(f"'acesso.params_janela.{k}' must be text.")
            else:
                _checar_placeholders(v, variaveis, f"acesso.params_janela.{k}", p)

    if classe == "portal_download":
        indice = a.get("pagina_indice")
        if not isinstance(indice, str) or not indice.lower().startswith("https://"):
            p.append("'acesso.pagina_indice' is required and must start with https://.")
        padrao = a.get("padrao_link")
        if not isinstance(padrao, str) or not padrao:
            p.append("'acesso.padrao_link' is required (regex of the link to download).")
        else:
            try:
                re.compile(padrao)
            except re.error as e:
                p.append(f"'acesso.padrao_link' is not a valid regular expression: {e}.")
        maxa = a.get("max_arquivos", 20)
        if not isinstance(maxa, int) or not 1 <= maxa <= 500:
            p.append("'acesso.max_arquivos' must be an integer between 1 and 500.")

    for campo in ("params", "headers"):
        v = a.get(campo)
        if v is not None and not isinstance(v, dict):
            p.append(f"'acesso.{campo}' must be an object.")


def _validar_limites(lim, p: list[str]) -> None:
    if lim is None:
        return
    if not isinstance(lim, dict):
        p.append("'limites' must be an object.")
        return
    faixas = {"timeout_segundos": (1, 900), "concorrencia": (1, 8), "tentativas": (1, 5)}
    for campo, (lo, hi) in faixas.items():
        if campo in lim and (not isinstance(lim[campo], int) or not lo <= lim[campo] <= hi):
            p.append(f"'limites.{campo}' must be an integer between {lo} and {hi}.")


# --- Typed access to the contract (defaults in one place) ---------------------------------------


def limite(contrato: dict, campo: str, padrao: int) -> int:
    return int((contrato.get("limites") or {}).get(campo, padrao))


def hash_contrato(contrato: dict) -> str:
    """sha256 of the canonical contract. It goes into the manifest so a batch says under WHICH
    contract it was produced (did the encoding change? yesterday's bronze came from another rule)."""
    canonico = json.dumps(contrato, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


# --- Window slicing ------------------------------------------------------------------------------


def piso_data(valor) -> date | None:
    """Accept YYYY, YYYY-MM and YYYY-MM-DD (the contract declares the granularity that fits)."""
    if isinstance(valor, date):
        return valor
    s = str(valor or "").strip()
    # Fill in the missing granularity and always parse a full date: `2026` becomes 01/01 and
    # `2026-08` becomes 08/01. The first day is the right floor: it is where that period's slice
    # starts.
    for complemento in ("", "-01", "-01-01"):
        try:
            return datetime.strptime(s + complemento, "%Y-%m-%d").date()
        except ValueError:
            continue
    return None


def _fim_do_mes(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def depende_da_fatia(contrato: dict) -> bool:
    """Does the request change with the requested period?

    A FIXED URL means the origin overwrites a single file and keeps no history. Asking for "from
    the 12th to the 14th" would download TODAY's file three times and label it with three dates:
    fake history, which is worse than missing history because it looks like data."""
    acesso = contrato.get("acesso") or {}
    textos = [str(acesso.get("url_padrao") or ""), str(acesso.get("pagina_indice") or "")]
    for mapa in ("params", "params_janela"):
        textos += [str(v) for v in (acesso.get(mapa) or {}).values()]
    for campo in ("param_inicio", "param_fim"):
        if acesso.get(campo):
            return True  # the window goes in a query parameter
    usados: set[str] = set()
    for t in textos:
        usados |= _placeholders(t)
    return bool(usados & set(CHAVES_TEMPORAIS))


def fatias(contrato: dict, inicio: date, fim: date) -> list[Fatia]:
    """Cut [inicio, fim] into the slices the contract defines, in chronological order.

    Honours `janela.inicio` (backfill floor: protects against asking for 1990 and hammering the
    origin) and `janela.atraso_dias` (the source publishes with a lag; the latest slice does not
    exist yet)."""
    j = contrato.get("janela") or {}
    tipo = j.get("tipo")
    piso = piso_data(j.get("inicio"))
    if piso and inicio < piso:
        inicio = piso
    atraso = int(j.get("atraso_dias", 0) or 0)
    if atraso:
        fim = fim - timedelta(days=atraso)
    if inicio > fim:
        return []

    out: list[Fatia] = []
    if tipo == "diaria":
        d = inicio
        while d <= fim:
            out.append(Fatia(chave=d.isoformat(), inicio=d, fim=d))
            d += timedelta(days=1)
    elif tipo == "mensal":
        d = inicio.replace(day=1)
        while d <= fim:
            out.append(Fatia(chave=f"{d.year:04d}-{d.month:02d}", inicio=d, fim=_fim_do_mes(d)))
            d = _fim_do_mes(d) + timedelta(days=1)
    elif tipo == "anual":
        for ano in range(inicio.year, fim.year + 1):
            out.append(Fatia(chave=f"{ano:04d}", inicio=date(ano, 1, 1), fim=date(ano, 12, 31)))
    elif tipo == "periodo":
        # FIXED GRID starting at the contract floor: the slice is NOT truncated at "today".
        #
        # Truncating made the key of the last slice change every day, and a new key is a new
        # batch: idempotency stopped holding exactly on the most reloaded slice, and a year of
        # monthly reloads would leave twelve overlapping windows in the bronze, which anyone
        # reading the whole dataset would add up. With the grid, the current slice has a stable
        # key and each reload becomes a VERSION of it (`DISTINCT ON (chave_lote)` already
        # resolves that).
        #
        # Asking the origin for the theoretical (future) end is safe for period sources: they
        # answer 200 with whatever exists up to today.
        passo = int(j.get("passo_max_dias") or 3650)
        grade = piso or inicio
        while grade + timedelta(days=passo) <= inicio:  # skip whole grids before the window
            grade += timedelta(days=passo)
        # `alinhar_a: "mes"` closes the slice on the LAST DAY OF THE MONTH. Without it, a series
        # whose point is dated on day 1 (a monthly series) lands in TWO slices: the 3,650-day grid
        # cuts on 12/29 and the next one starts on 12/30, and the origin returns the December
        # point (dated 12/01) in both queries. The same point becomes two batches with different
        # `chave_lote`, and both `DISTINCT ON (chave_lote)` and the unique index
        # `(fonte, chave, sha)` only protect WITHIN a key, so the duplicate slips through.
        # `cadencia` is no signal: it is the frequency of EXECUTION, not the grain of the series.
        alinhar = str(j.get("alinhar_a") or "").lower()
        ini = grade
        while ini <= fim:
            f = ini + timedelta(days=passo - 1)
            if alinhar == "mes":
                f = date(f.year + (f.month == 12), 1 if f.month == 12 else f.month + 1, 1)
                f -= timedelta(days=1)
            out.append(Fatia(chave=f"{ini.isoformat()}..{f.isoformat()}", inicio=ini, fim=f))
            ini = f + timedelta(days=1)
    else:  # a validated contract never gets here; defensive
        raise ErroAplicacao(
            codigo="janela_invalida",
            mensagem=f"Unsupported window type: {tipo}.",
            status=422,
        )
    # Single overwritten file: only the latest slice. All the others would download the SAME file
    # and label it with dates on which it did not exist.
    if out and not depende_da_fatia(contrato):
        out = out[-1:]
    return out


# --- URL / parameter resolution -----------------------------------------------------------


def resolver(texto: str, fatia: Fatia, variaveis: dict | None = None) -> str:
    """Replace `{ano}`, `{mes}`, `{dia}`, `{ano_mes}`, `{data}`, `{inicio[:fmt]}`, `{fim[:fmt]}`,
    `{chave}` and the contract `variaveis`.

    Controlled substitution (own regex), not `str.format`: the pattern comes from registered data
    and `format` would give access to attributes (`{x.__class__}`) and break on any literal brace."""
    ctx: dict[str, object] = {
        "ano": f"{fatia.inicio.year:04d}",
        # Two-digit year, for sources that name daily files like `daily_{yy}{mm}{dd}.txt`.
        "ano_curto": f"{fatia.inicio.year % 100:02d}",
        "mes": f"{fatia.inicio.month:02d}",
        "dia": f"{fatia.inicio.day:02d}",
        "ano_mes": f"{fatia.inicio.year:04d}{fatia.inicio.month:02d}",
        "data": fatia.inicio.isoformat(),
        "chave": fatia.chave,
        "inicio": fatia.inicio,
        "fim": fatia.fim,
    }
    ctx.update({k: str(v) for k, v in (variaveis or {}).items()})

    def _sub(m: re.Match) -> str:
        nome, fmt = m.group(1), m.group(2)
        if nome not in ctx:
            raise ErroAplicacao(
                codigo="placeholder_desconhecido",
                mensagem=f"Placeholder '{{{nome}}}' does not exist in the contract.",
                status=422,
            )
        valor = ctx[nome]
        if fmt:
            if isinstance(valor, date):
                return valor.strftime(fmt)
            raise ErroAplicacao(
                codigo="placeholder_invalido",
                mensagem=f"Date format applied to '{{{nome}}}', which is not a date.",
                status=422,
            )
        return valor.isoformat() if isinstance(valor, date) else str(valor)

    return _PLACEHOLDER.sub(_sub, texto)
