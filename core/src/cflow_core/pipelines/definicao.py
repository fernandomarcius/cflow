"""Graph definition and structural validation.

definicao = {"nos": [{"id","tipo","sql"?,"config"?,"rotulo"?}], "arestas": [{"de","para"}]}.
The node `id` is stable (renaming the `rotulo` does not change the `id`).
"""

from __future__ import annotations

import json
import re
from datetime import date

from ..errors import DetalheErro, ErroAplicacao
from ..execucao.paralelo import serial as _no_serial
from ..periodos import (
    deslocar_competencia,
    eh_competencia,
    eh_relativo,
    resolver_corte,
    resolver_janela,
)

# Runtime parameters of the pipeline.
# Declared in the definition as `definicao["parametros"]`; referenced in the node SQL as `:name`.
# - "competencia": a period month (string "YYYY-MM" | "atual" | "anterior"). A single parameter
#   named `X` is EXPANDED into two boundary dates injected into the node SQL as `:X_inicio` (1st
#   of the month) and `:X_fim` (1st of the next month), in ISO YYYY-MM-DD, forming the half-open
#   window [start, end).
# A parameter MAY carry `derivacao` (computed from another parameter); in that case the operator
# does not fill it in. Two families: by MAP (`arquivo`/`fonte`/`no`, read in the backend and in the
# Portal) and CALENDAR (`origem: "competencia"`, the period `de` shifted by `meses` months,
# computed by the platform; see `resolver_derivados_competencia`). The derivation lives in the
# parameter DEFINITION, in one place (not split between pipeline and page). See `_validar_derivacao`.
# `as_of`: SYSTEM time cut (data_registro): sees the data "as it was" up to an instant.
# Value = period token / 'YYYY-MM' / date / timestamp; resolved by `resolver_corte` into an
# inclusive timestamp and injected as `:{name}` (the source node filters `REGISTERED_AT <= :{name}`).
# Bitemporal: orthogonal to `competencia` (BUSINESS time). Opt-in: pipelines without it do not change.
TIPOS_PARAMETRO = ("data", "texto", "inteiro", "opcoes", "competencia", "as_of")

# `segredo: true` marks a parameter that carries a CREDENTIAL (e.g. the password an API requires
# in the authentication body). The value still flows at runtime (the API needs it) but is REDACTED
# everywhere that persists: `execucoes.parametros`, the audit trail (append-only and permanent) and
# API responses.
# A name matching this pattern requires the mark (see `_validar_parametros`), so carelessness does not leak.
_NOME_SEGREDO = re.compile(
    r"(senha|password|passwd|pwd|secret|segredo|token|api[_-]?key|apikey|credencial|credential)",
    re.IGNORECASE,
)
REDIGIDO = "***"  # marker stored in place of the value


def nome_parece_segredo(nome: str) -> bool:
    """Parameter name that looks like it carries a credential."""
    return bool(_NOME_SEGREDO.search(nome or ""))


def nomes_segredo(definicao: dict) -> set[str]:
    """Names of parameters marked `segredo: true` in the definition."""
    return {
        p["nome"]
        for p in parametros_declarados(definicao)
        if isinstance(p, dict) and isinstance(p.get("nome"), str) and p.get("segredo") is True
    }


def redigir_segredos(definicao: dict, valores: dict | None) -> dict:
    """Copy of `valores` with the `segredo: true` parameters replaced by `REDIGIDO`.

    Use ALWAYS when parameters are persisted, audited or returned by the API. The real value
    stays only in memory, on the request path that needs it."""
    if not valores:
        return dict(valores or {})
    segredos = nomes_segredo(definicao)
    return {k: (REDIGIDO if k in segredos else v) for k, v in valores.items()}


_NOME_PARAM = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")  # valid identifier for a :name placeholder
_SLUG = re.compile(r"^[A-Za-z0-9_-]+$")  # published dataset name (safe file name)
#: A LAKE dataset accepts `/` to group by family (`sales/orders`, `ref/summary`). The bucket has
#: no folders (the `/` is just part of the key), and many sibling datasets at the root were
#: unreadable in the console. Each segment is still the usual slug, and that is what rules out
#: `..`, `//`, leading/trailing slashes and empty segments: nothing is silently sanitized, it is
#: refused. Capped at 3 levels: deeper is a hierarchy nobody navigates.
_SLUG_LAKE = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+){0,2}$")
# SQL identifier (schema/table/target column/key) of the `escrita_sql` node. Anti-injection: the
# generated SQL always quotes the identifier, but validation refuses anything outside this alphabet.
_IDENT_SQL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_METODOS_HTTP = ("GET", "POST", "PUT", "PATCH", "DELETE")
_ESTRATEGIAS_ESCRITA = ("append", "upsert", "truncate_insert")
# Cap on attachments per e-mail: SMTP servers refuse large messages, and 20 files in an e-mail is
# already a sign the design called for an output node, not an attachment.
_MAX_ANEXOS = 20
# SQL string literal (single quotes, with '' escaped). Used to strip literals before looking for an
# inner `;`: a ';' INSIDE a string ('a;b') is data, not a statement separator.
_SQL_LITERAL = re.compile(r"'(?:[^']|'')*'")


def _msg_competencia(prefixo: str) -> str:
    return f"{prefixo} must be a period 'YYYY-MM', 'atual' or 'anterior'."


def _competencia_janela(valor: str) -> tuple[str, str]:
    """Resolve a period value ("YYYY-MM" | "atual" | "anterior") into the half-open window
    [start, end): (1st of the month, 1st of the next month) in ISO YYYY-MM-DD.

    "atual"/"anterior" are relative to the current month in the configured time zone (explicit).
    Delegates to `periodos.resolver_janela` (single source of date resolution) but keeps the
    STRICT period contract: month only ('atual'/'anterior'/'YYYY-MM'); week/day tokens are out.
    Raises ValueError if the value is not recognized (the caller converts it into an error)."""
    v = str(valor)
    # The SAME predicate as the calendar derivation (`periodos.eh_competencia`): a period accepted
    # here and refused by the derivation would leave the derived value empty, and the SQL would bind NULL.
    if not eh_competencia(v):
        raise ValueError(valor)
    return resolver_janela(v)


def _erro(detalhes: list[DetalheErro]) -> ErroAplicacao:
    return ErroAplicacao(
        codigo="grafo_invalido",
        mensagem="The graph has structural errors.",
        status=422,
        detalhes=detalhes,
    )


def parametros_declarados(definicao: dict) -> list[dict]:
    """List (possibly empty) of the parameters declared in the definition."""
    params = definicao.get("parametros", []) if isinstance(definicao, dict) else []
    return params if isinstance(params, list) else []


def filtrar_parametros_declarados(definicao: dict, valores: dict | None) -> dict:
    """Cut `valores` down to the parameters DECLARED in the definition (by name). Used when
    chaining pipelines in a DAG: the SAME runtime values flow to the whole chain, but each
    pipeline only knows the ones it declares, since an upstream pipeline may declare a subset of
    the root's parameters. Without this cut, `normalizar_parametros` would reject the undeclared
    value (400 parametros_invalidos)."""
    if not valores:
        return {}
    nomes = {
        p["nome"]
        for p in parametros_declarados(definicao)
        if isinstance(p, dict) and isinstance(p.get("nome"), str)
    }
    return {k: v for k, v in valores.items() if k in nomes}


def validar_parametros_da_cadeia(definicoes: list[dict], valores: dict | None) -> None:
    """Refuse a parameter name that NO pipeline of the chain declares (400 `parametros_invalidos`).

    `filtrar_parametros_declarados` cuts per pipeline, which is what lets the upstream declare a
    subset. But cutting N times makes a name nobody declares vanish SILENTLY: a typo in
    `competencia` would run a job overnight without the period cut, green, plausible and wrong,
    which is exactly the outcome chaining exists to prevent. For a SINGLE pipeline this has always
    been a loud error (`normalizar_parametros`); the chain must keep the same bar, checking
    against the UNION of the declared names instead of against one.
    """
    if not valores:
        return
    declarados: set[str] = set()
    for d in definicoes:
        declarados |= {
            p["nome"]
            for p in parametros_declarados(d)
            if isinstance(p, dict) and isinstance(p.get("nome"), str)
        }
    orfaos = sorted(set(valores) - declarados)
    if orfaos:
        raise ErroAplicacao(
            codigo="parametros_invalidos",
            mensagem="Parameter not declared by any pipeline in the chain.",
            status=400,
            detalhes=[
                DetalheErro(campo="parametros", mensagem=f"Undeclared parameter: {k!r}.")
                for k in orfaos
            ],
        )


def completar_declarados(definicao: dict, valores: dict | None) -> dict:
    """Effective map to BIND in the SQL: every declared parameter present, with `None` for those
    that received no value. `normalizar_parametros` OMITS an optional parameter with no value
    (right for persisting and auditing), but whoever binds needs the key: without it the `:name`
    would stay literal in the SQL and the engine would fail on syntax, instead of falling into
    "blank = everything" (neutralized predicate)."""
    eff = dict(valores or {})
    for p in parametros_declarados(definicao):
        if not isinstance(p, dict):
            continue
        nome = p.get("nome")
        if not isinstance(nome, str) or not nome:
            continue
        if p.get("tipo") == "competencia":
            # `competencia` EXPANDS into :{name}_inicio/:{name}_fim; the raw name is not a bind.
            eff.setdefault(f"{nome}_inicio", None)
            eff.setdefault(f"{nome}_fim", None)
            continue
        eff.setdefault(nome, None)
    return eff


def _base_competencia(valores: dict, de: str, p_de: dict | None) -> str | None:
    """Where the calendar derivation comes from: the RAW period value (`2026-08`, `atual`), the
    EXPANDED window of exactly one month (`<de>_inicio`/`<de>_fim`, which is how re-execution,
    that passes the stored parameters, and the preview arrive) or the period default. The "all"
    window of a preview with no value is not a period at all: there is no base then, and the
    derived value stays empty."""
    bruto = valores.get(de)
    if bruto not in (None, ""):
        return str(bruto)
    ini, fim = valores.get(f"{de}_inicio"), valores.get(f"{de}_fim")
    if isinstance(ini, str) and isinstance(fim, str):
        try:
            if resolver_janela(ini[:7]) == (ini, fim):
                return ini[:7]
        except ValueError:
            pass
    padrao = (p_de or {}).get("default")
    return str(padrao) if padrao not in (None, "") else None


def resolver_derivados_competencia(
    definicao: dict,
    valores: dict | None,
    *,
    estrito: bool = True,
    hoje: date | None = None,
) -> dict:
    """Return `valores` with the CALENDAR derived values computed:
    `{"derivacao": {"origem": "competencia", "de": "competencia", "meses": -1}}` is the period
    `de` shifted by `meses` months, on day 1 (`2026-08` -> `2026-07-01`).

    Pure computation, no I/O: runs in execution (before normalization, in `resolver_derivados`),
    in the preview and for whoever only checks a payload. With `estrito` (the default), a value
    SUPPLIED for the derived parameter must equal the computed one; different is
    `400 parametro_derivado_divergente`, never "the supplied one wins": a hand-written derived
    period would make the average read the history of another month, plausible and wrong. Without
    `estrito` (the Studio preview) the supplied value wins, as the preview value promises. An
    invalid period does not become a derived value: normalization flags it, by its own name."""
    valores = dict(valores or {})
    declarados = {
        p["nome"]: p
        for p in parametros_declarados(definicao)
        if isinstance(p, dict) and isinstance(p.get("nome"), str)
    }
    divergentes: list[DetalheErro] = []
    for nome, p in declarados.items():
        d = p.get("derivacao")
        if not isinstance(d, dict) or d.get("origem") != "competencia":
            continue
        de, meses = d.get("de"), d.get("meses")
        if not isinstance(de, str) or not isinstance(meses, int) or isinstance(meses, bool):
            continue  # invalid shape: structural validation refuses it; no value is invented here
        base = _base_competencia(valores, de, declarados.get(de))
        calculado: str | None = None
        if base is not None:
            try:
                calculado = deslocar_competencia(base, meses, hoje)
            except ValueError:
                # A period that normalization ACCEPTS and whose shift falls outside the calendar
                # (`0001-01` with -1) must not leave the derived value empty: the SQL would bind
                # NULL, which means "bring everything". One it refuses is left for it to flag, by name.
                try:
                    _competencia_janela(base)
                except ValueError:
                    calculado = None
                else:
                    raise ErroAplicacao(
                        codigo="parametro_derivado_invalido",
                        mensagem="Derived parameter has no possible value for this period.",
                        status=400,
                        detalhes=[
                            DetalheErro(
                                campo="parametros",
                                mensagem=(
                                    f"'{nome}': the period {base!r} shifted by {meses:+d} "
                                    "months falls outside the calendar (years 1 to 9999)."
                                ),
                            )
                        ],
                    ) from None
        informado = valores.get(nome)
        if informado in (None, ""):
            if calculado is not None:
                valores[nome] = calculado
            continue
        if estrito and calculado is not None and str(informado) != calculado:
            divergentes.append(
                DetalheErro(
                    campo="parametros",
                    mensagem=(
                        f"'{nome}' came as {informado!r}, but the rule ('{de}' {meses:+d} months) gives "
                        f"{calculado!r}. Leave it blank: the platform computes it."
                    ),
                )
            )
    if divergentes:
        raise ErroAplicacao(
            codigo="parametro_derivado_divergente",
            mensagem="Derived parameter supplied differs from what the platform computes.",
            status=400,
            detalhes=divergentes,
        )
    return valores


def parametros_relativos(definicao: dict, valores: dict | None) -> list[str]:
    """Names whose value, in THIS input, depends on the day it is resolved: period
    `atual`/`anterior` and as-of cut `hoje`/`ontem`/`mes_atual`... (the supplied one or, if blank,
    the default) and the calendar derivation computed from such a period. The same input
    normalizes differently tomorrow: whoever recognizes an execution by its parameters cannot
    trust them. Only `competencia` and `as_of` resolve tokens; in `texto`, "hoje" is the word hoje."""
    valores = valores or {}
    declarados = [
        p
        for p in parametros_declarados(definicao)
        if isinstance(p, dict) and isinstance(p.get("nome"), str)
    ]
    relativos: set[str] = set()
    for p in declarados:
        if p.get("tipo") not in ("competencia", "as_of"):
            continue
        v = valores.get(p["nome"])
        if v in (None, ""):
            v = p.get("default")
        if eh_relativo(v):
            relativos.add(p["nome"])
    for p in declarados:
        d = p.get("derivacao")
        if isinstance(d, dict) and d.get("origem") == "competencia" and d.get("de") in relativos:
            relativos.add(p["nome"])
    return sorted(relativos)


def bibliotecas_usadas(definicao: dict) -> list[str]:
    """Ids (uuid as text) of the function libraries used: PIPELINE level (`definicao.bibliotecas`,
    backwards compatible) + per NODE (`no.config.bibliotecas`). Union preserving first-appearance
    order: the author picks the function in the node that uses it; preview/execution load the
    set that is needed."""
    vistos: list[str] = []

    def _add(lst: object) -> None:
        if isinstance(lst, list):
            for b in lst:
                s = str(b).strip()
                if s and s not in vistos:
                    vistos.append(s)

    if isinstance(definicao, dict):
        _add(definicao.get("bibliotecas"))
        for n in definicao.get("nos") or []:
            if isinstance(n, dict):
                _add((n.get("config") or {}).get("bibliotecas"))
    return vistos


def normalizar_parcial(definicao: dict, valores: dict) -> dict:
    """Normalize ONLY the supplied keys, to compare by CONTAINMENT (`@>`) with
    `execucoes.parametros`, which is stored already normalized.

    It exists because there is a question `normalizar_parametros` cannot answer: *"is there an
    execution from today for unit X?"*. Its as-of is precisely what is not known yet (it is what
    we want to find out), and `normalizar_parametros` would apply defaults and demand the missing
    required parameter, answering `400` to a legitimate question.

    The coercion rules are the SAME: instead of rewriting them, the definition is cut down to the
    supplied parameters, with no `default`, no `obrigatorio` and no `derivacao`, and the usual
    normalization runs over that cut. Two copies of the rules would diverge the day a new type
    came in, and the symptom would be a filter that silently does not match.

    `competencia` still expands into `{name}_inicio`/`{name}_fim`, which is how it is stored.

    Refuses an empty dict on purpose: `@>` with `{}` matches ANY execution, so an empty filter is
    not "no filter", it is "all", and whoever asked would read that as "none".
    """
    if not valores:
        raise ErroAplicacao(
            codigo="parametros_invalidos",
            mensagem="Provide at least one parameter to look up the execution.",
            status=400,
            detalhes=[
                DetalheErro(
                    campo="parametros",
                    mensagem="An empty filter would match any execution of the day.",
                )
            ],
        )
    declarados = {
        p["nome"]: p
        for p in parametros_declarados(definicao)
        if isinstance(p, dict) and isinstance(p.get("nome"), str)
    }
    recorte = []
    for nome in valores:
        p = declarados.get(nome)
        if p is None:
            continue  # undeclared key: `normalizar_parametros` below is the one that refuses it
        magro = {k: v for k, v in p.items() if k not in ("default", "obrigatorio", "derivacao")}
        recorte.append(magro)
    return normalizar_parametros({**definicao, "parametros": recorte}, valores)


def normalizar_parametros(definicao: dict, valores: dict | None) -> dict:
    """Validate the supplied VALUES against the DECLARED parameters; apply defaults; return the
    normalized dict (declared ones only). Raises ErroAplicacao(400, 'parametros_invalidos') if a
    required value is missing, an undeclared one is sent, or a type/option is invalid."""
    declarados = {
        p["nome"]: p
        for p in parametros_declarados(definicao)
        if isinstance(p, dict) and isinstance(p.get("nome"), str)
    }
    valores = valores or {}
    detalhes: list[DetalheErro] = []
    for k in valores:
        if k not in declarados:
            detalhes.append(
                DetalheErro(campo="parametros", mensagem=f"Undeclared parameter: {k!r}.")
            )

    out: dict = {}
    for nome, p in declarados.items():
        tipo = p.get("tipo")
        # A BLANK field is "no value", not the value "": same rule the preview already uses
        # (`preview._params_preview`). The Portal sends "" for an unfilled filter and the Studio
        # modal OMITS it: without this equivalence the two surfaces diverged, and a blank
        # `texto`/`data` bound "" and silently returned 0 rows (the preview showed everything).
        if nome in valores and valores[nome] is not None and valores[nome] != "":
            v = valores[nome]
        elif p.get("default") is not None:
            v = p["default"]
        elif p.get("obrigatorio") and not p.get("derivacao"):
            # A DERIVED parameter is computed by the caller (the Portal resolves it from the map);
            # never demanded from the operator, even if marked required.
            detalhes.append(
                DetalheErro(campo="parametros", mensagem=f"Missing required parameter: {nome}.")
            )
            continue
        else:
            continue  # optional/derived with no value: not included
        if tipo == "inteiro":
            try:
                v = int(v)
            except (TypeError, ValueError):
                detalhes.append(
                    DetalheErro(campo="parametros", mensagem=f"'{nome}' must be an integer.")
                )
                continue
        elif tipo == "opcoes":
            # Static options: the value must belong to the declared list.
            # Dynamic options (`opcoes_fonte`): the list comes from a connection and is resolved
            # on the client; the backend does not know it, so it accepts the value as text
            # (lenient validation).
            if p.get("opcoes_fonte"):
                v = str(v)
            elif v not in (p.get("opcoes") or []):
                detalhes.append(
                    DetalheErro(
                        campo="parametros", mensagem=f"Value of '{nome}' is not among the options."
                    )
                )
                continue
        elif tipo == "competencia":
            # "YYYY-MM" | "atual" | "anterior" -> EXPANDS into :{name}_inicio / :{name}_fim
            # (half-open window), which is what the node SQL references. Does not inject `:{name}`.
            try:
                inicio, fim = _competencia_janela(str(v))
            except ValueError:
                msg = _msg_competencia(f"'{nome}'")
                detalhes.append(DetalheErro(campo="parametros", mensagem=msg))
                continue
            out[f"{nome}_inicio"] = inicio
            out[f"{nome}_fim"] = fim
            continue
        elif tipo == "as_of":
            # As-of cut (system time) -> a single inclusive timestamp, injected as :{name}.
            try:
                out[nome] = resolver_corte(str(v))
            except ValueError:
                detalhes.append(
                    DetalheErro(
                        campo="parametros",
                        mensagem=f"'{nome}' (as_of) must be a date/time or a period token.",
                    )
                )
            continue
        else:  # texto / data -> string
            v = str(v)
        out[nome] = v

    if detalhes:
        raise ErroAplicacao(
            codigo="parametros_invalidos",
            mensagem="Invalid execution parameters.",
            status=400,
            detalhes=detalhes,
        )
    return out


def _validar_parametros(definicao: dict, detalhes: list[DetalheErro]) -> None:
    """Accumulate errors of the declared runtime parameters (optional block, backwards compatible)."""
    params = definicao.get("parametros")
    if params is None:
        return  # no parameters: valid definition (backwards compatible)
    if not isinstance(params, list):
        detalhes.append(
            DetalheErro(campo="parametros", mensagem="Invalid format (expected a list).")
        )
        return
    nomes: list[str] = []
    # The calendar derivation checks the SOURCE parameter (type, required-ness), which may be
    # declared after it in the list.
    declarados = {
        q["nome"]: q for q in params if isinstance(q, dict) and isinstance(q.get("nome"), str)
    }
    for p in params:
        if not isinstance(p, dict):
            detalhes.append(
                DetalheErro(campo="parametros", mensagem="Each parameter must be an object.")
            )
            continue
        nome = p.get("nome")
        if not nome or not isinstance(nome, str) or not _NOME_PARAM.match(nome):
            detalhes.append(
                DetalheErro(campo="parametros", mensagem=f"Invalid parameter name: {nome!r}.")
            )
        else:
            nomes.append(nome)
        tipo = p.get("tipo")
        if tipo not in TIPOS_PARAMETRO:
            detalhes.append(
                DetalheErro(
                    campo="parametros",
                    mensagem=f"Invalid type for '{nome}': {tipo!r} (use {list(TIPOS_PARAMETRO)}).",
                )
            )
        # A name that looks like a credential must be marked `segredo: true`: a parameter value is
        # PERSISTED (execucoes.parametros) and goes to the audit trail, which is append-only and
        # permanent. When marked, it is redacted in both places (`redigir_segredos`); without the
        # mark, it would be stored in clear text forever. We do not refuse the parameter: some APIs
        # require the credential in the request body, and refusing would leave the author stuck.
        if isinstance(nome, str) and nome_parece_segredo(nome) and not p.get("segredo"):
            detalhes.append(
                DetalheErro(
                    campo="parametros",
                    mensagem=(
                        f"'{nome}' looks like a credential: set \"segredo\": true so the value is not "
                        "stored in the audit trail (which is permanent)."
                    ),
                )
            )
        if "segredo" in p and not isinstance(p.get("segredo"), bool):
            detalhes.append(
                DetalheErro(
                    campo="parametros", mensagem=f"'{nome}': \"segredo\" must be true/false."
                )
            )
        default = p.get("default")
        if tipo == "opcoes":
            # Two sources of options, mutually exclusive:
            #   - static: a non-empty `opcoes` list (validated here, with default in opcoes);
            #   - dynamic: `opcoes_fonte`, the options come from a CONNECTION or a pipeline NODE.
            #       - connection, legacy: {conexao_id, coluna} (value = label; reads a distinct column);
            #       - connection, label != value/DB: {conexao_id, coluna_valor, coluna_rotulo?, sql?}
            #         (shows a nickname, sends the id). Resolved at /conexoes/{id}/opcoes.
            #       - node: {origem:"no", no_id, coluna_valor, coluna_rotulo?}; the node output
            #         becomes the dropdown. Resolved at /pipelines/{id}/opcoes-no.
            #     Only the format is checked here; the values are not known at validation time.
            fonte = p.get("opcoes_fonte")
            opcoes = p.get("opcoes")
            origem_fonte = fonte.get("origem", "conexao") if isinstance(fonte, dict) else "conexao"
            if origem_fonte == "no":
                tem_fonte = (
                    isinstance(fonte, dict)
                    and bool(fonte.get("no_id"))
                    and bool(fonte.get("coluna_valor") or fonte.get("coluna"))
                )
                msg_fonte = f"Parameter '{nome}' (opcoes_fonte origem 'no') requires {{no_id, coluna_valor}}."
            else:
                tem_fonte = (
                    isinstance(fonte, dict)
                    and bool(fonte.get("conexao_id"))
                    and bool(fonte.get("coluna") or fonte.get("coluna_valor"))
                )
                msg_fonte = f"Parameter '{nome}' (opcoes_fonte) requires {{conexao_id, coluna}} or {{conexao_id, coluna_valor}}."
            if fonte is not None and not tem_fonte:
                detalhes.append(DetalheErro(campo="parametros", mensagem=msg_fonte))
            elif tem_fonte:
                if isinstance(opcoes, list) and opcoes:
                    detalhes.append(
                        DetalheErro(
                            campo="parametros",
                            mensagem=f"Parameter '{nome}': use 'opcoes' OR 'opcoes_fonte', not both.",
                        )
                    )
            elif not isinstance(opcoes, list) or not opcoes:
                detalhes.append(
                    DetalheErro(
                        campo="parametros",
                        mensagem=f"Parameter '{nome}' (opcoes) requires a non-empty 'opcoes' list or 'opcoes_fonte'.",
                    )
                )
            elif default is not None and default not in opcoes:
                detalhes.append(
                    DetalheErro(
                        campo="parametros",
                        mensagem=f"Default of '{nome}' is not among the options.",
                    )
                )
        elif tipo == "inteiro" and default is not None and not isinstance(default, int):
            detalhes.append(
                DetalheErro(campo="parametros", mensagem=f"Default of '{nome}' must be an integer.")
            )
        elif tipo == "competencia" and default is not None:
            try:
                _competencia_janela(str(default))
            except ValueError:
                msg = _msg_competencia(f"Default of '{nome}'")
                detalhes.append(DetalheErro(campo="parametros", mensagem=msg))
        elif tipo == "as_of" and default is not None:
            try:
                resolver_corte(str(default))
            except ValueError:
                detalhes.append(
                    DetalheErro(
                        campo="parametros",
                        mensagem=f"Default of '{nome}' (as_of) must be a date/time or a period token.",
                    )
                )
        # Optional derivation: the parameter is COMPUTED from another parameter (by map or by
        # calendar), not filled in by the operator. Map: LENIENT validation, structure only.
        if "derivacao" in p:
            _validar_derivacao(nome, p.get("derivacao"), detalhes, param=p, declarados=declarados)
    if len(set(nomes)) != len(nomes):
        detalhes.append(DetalheErro(campo="parametros", mensagem="Duplicate parameter names."))


def _validar_bibliotecas(definicao: dict, detalhes: list[DetalheErro]) -> None:
    """OPTIONAL block: `definicao["bibliotecas"]` = list of ids (uuid as text) of the function
    libraries the pipeline uses. Absent is valid (backwards compatible). If present, it must be a
    list of strings; the content (existence/publication) is resolved at execution/preview, not here."""
    bibs = definicao.get("bibliotecas")
    if bibs is None:
        return  # no libraries: valid definition (backwards compatible)
    if not isinstance(bibs, list) or any(not isinstance(b, str) or not b.strip() for b in bibs):
        detalhes.append(
            DetalheErro(campo="bibliotecas", mensagem="Expected a list of ids (uuid as text).")
        )


_ORIGENS_DERIVACAO = ("arquivo", "fonte", "no", "competencia")
# Maximum shift of the calendar derivation, in months. The real case is -1/-2 (a moving average);
# ten years each way covers any history without accepting a number that is a typo.
_MESES_DERIVACAO_MAX = 120
# The calendar derivation value is the 1st day of the month in ISO ("YYYY-MM-01"): text for the
# partition and a date for the SQL. `competencia`/`as_of`/`opcoes`/`inteiro` would reinterpret it.
_TIPOS_DERIVADO_COMPETENCIA = ("texto", "data")


def _validar_derivacao_competencia(
    nome: str | None, der: dict, detalhes: list[DetalheErro], param: dict, declarados: dict
) -> None:
    """`{origem: "competencia", de, meses}`: the value is the period `de` shifted by `meses`
    months, on day 1, computed by the PLATFORM, not by whoever triggers. Unlike the map, the
    validation here is not lenient: each refusal closes a way for the derived value to come out
    empty (and the SQL to bind NULL, which means "bring everything") or with a fixed value nobody
    recomputes."""

    def erro(msg: str) -> None:
        detalhes.append(DetalheErro(campo="parametros", mensagem=f"Derivation of '{nome}': {msg}"))

    de = der.get("de")
    p_de = declarados.get(de) if isinstance(de, str) else None
    if not isinstance(de, str) or not de.strip():
        erro("requires 'de', the period parameter the value comes from.")
    elif p_de is None:
        erro(f"'de' points to '{de}', which is not a declared parameter.")
    elif p_de.get("tipo") != "competencia":
        erro(f"'de' points to '{de}', which is not of type competencia.")
    elif not p_de.get("obrigatorio") and p_de.get("default") in (None, ""):
        erro(
            f"the period '{de}' must be required or have a default value; if blank, the "
            "derived value would have nothing to come from and the SQL would read every month."
        )
    meses = der.get("meses")
    if (
        not isinstance(meses, int)
        or isinstance(meses, bool)
        or meses == 0
        or abs(meses) > _MESES_DERIVACAO_MAX
    ):
        erro(
            f"'meses' must be a non-zero integer between -{_MESES_DERIVACAO_MAX} and "
            f"{_MESES_DERIVACAO_MAX} (e.g. -1 for the previous period)."
        )
    if param.get("tipo") not in _TIPOS_DERIVADO_COMPETENCIA:
        erro("the derived parameter must be of type texto or data (the value is 'YYYY-MM-01').")
    if param.get("default") not in (None, ""):
        erro(
            "does not accept a default value: the value is a function of the period, and a fixed "
            "default would override it on every run."
        )
    if param.get("segredo") is True:
        erro("cannot be a credential (segredo).")


def _validar_derivacao(
    nome: str | None,
    der,
    detalhes: list[DetalheErro],
    *,
    param: dict | None = None,
    declarados: dict | None = None,
) -> None:
    """Validation of `parametro.derivacao` (optional block, backwards compatible).

    Shape: { origem?, de, campo_id, campo_valor, normalizar?: "sistema", juntar?, ... } where the
    map ORIGIN varies:
      - `arquivo` (default, backwards compatible): requires `mapa` (name of an `arquivo` connection).
      - `fonte`: requires `conexao_id` (uuid as text); `sql` optional (query on the connection).
      - `no`: requires `no_id` (id of a node of the pipeline ITSELF, whose output becomes the map).
    For the three, only structure/required fields are checked (lenient); resolution (reading the
    map, filtering by origin) happens in the backend (`derivacao.resolver_*`) and/or in the Portal.
      - `competencia`: NO map, `{origem, de, meses}`; see `_validar_derivacao_competencia`,
        which is not lenient.
    """
    if not isinstance(der, dict):
        detalhes.append(
            DetalheErro(campo="parametros", mensagem=f"Derivation of '{nome}' must be an object.")
        )
        return
    origem = der.get("origem", "arquivo")
    if origem == "competencia":
        _validar_derivacao_competencia(nome, der, detalhes, param or {}, declarados or {})
        return
    if origem not in _ORIGENS_DERIVACAO:
        detalhes.append(
            DetalheErro(
                campo="parametros",
                mensagem=f"Derivation of '{nome}': 'origem' only accepts {list(_ORIGENS_DERIVACAO)}.",
            )
        )
        origem = "arquivo"  # keep validating the common fields with the default shape
    # ALWAYS required fields (independent of the origin): what to filter by and which column to derive.
    comuns = ("de", "campo_id", "campo_valor")
    # MAP field, specific to each origin.
    especifico = {"arquivo": ("mapa",), "fonte": ("conexao_id",), "no": ("no_id",)}[origem]
    for chave in comuns + especifico:
        v = der.get(chave)
        if not v or not isinstance(v, str) or not v.strip():
            detalhes.append(
                DetalheErro(
                    campo="parametros",
                    mensagem=f"Derivation of '{nome}' requires '{chave}' (non-empty text).",
                )
            )
    # `sql` (origin `fonte`) is optional; if present, it must be text.
    if der.get("sql") is not None and not isinstance(der.get("sql"), str):
        detalhes.append(
            DetalheErro(campo="parametros", mensagem=f"Derivation of '{nome}': 'sql' must be text.")
        )
    if "normalizar" in der and der.get("normalizar") not in (None, "sistema"):
        detalhes.append(
            DetalheErro(
                campo="parametros",
                mensagem=f"Derivation of '{nome}': 'normalizar' only accepts \"sistema\".",
            )
        )
    if "juntar" in der and der.get("juntar") is not None and not isinstance(der.get("juntar"), str):
        detalhes.append(
            DetalheErro(
                campo="parametros", mensagem=f"Derivation of '{nome}': 'juntar' must be text."
            )
        )


def _faixa_inteiro(
    cfg: dict, campo: str, lo: int, hi: int, nid, rotulo: str, detalhes: list[DetalheErro]
) -> None:
    """If `campo` is present, require an integer in [lo, hi]. Absent = uses the default (not validated)."""
    if campo not in cfg or cfg[campo] is None:
        return
    v = cfg[campo]
    if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=f"{rotulo} '{nid}': '{campo}' must be an integer between {lo} and {hi}.",
            )
        )


def _validar_escrita_sql(nid, cfg: dict, definicao: dict, detalhes: list[DetalheErro]) -> None:
    """Validate an `escrita_sql` node (effect). Connection/flag compatibility is checked at publish
    time (`validar_conexoes_efeito`), not here: the structure applies even to a draft."""
    if not cfg.get("conexao_id"):
        detalhes.append(
            DetalheErro(campo="nos", mensagem=f"Write '{nid}' requires config.conexao_id.")
        )
    modo = cfg.get("modo")
    if modo not in ("tabela", "sql"):
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=f"Write '{nid}': invalid modo '{modo}' (use 'tabela' or 'sql').",
            )
        )
    if modo == "tabela":
        for campo in ("schema", "tabela"):
            v = cfg.get(campo)
            if not v or not isinstance(v, str) or not _IDENT_SQL.match(v):
                detalhes.append(
                    DetalheErro(
                        campo="nos",
                        mensagem=f"Write '{nid}': '{campo}' must be a simple SQL identifier.",
                    )
                )
        estrategia = cfg.get("estrategia", "append")
        if estrategia not in _ESTRATEGIAS_ESCRITA:
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=f"Write '{nid}': invalid estrategia '{estrategia}' "
                    f"(use {list(_ESTRATEGIAS_ESCRITA)}).",
                )
            )
        chave = cfg.get("chave")
        if estrategia == "upsert":
            if not chave or not isinstance(chave, list):
                detalhes.append(
                    DetalheErro(
                        campo="nos",
                        mensagem=f"Write '{nid}': estrategia 'upsert' requires config.chave (a list of columns).",
                    )
                )
            else:
                for c in chave:
                    if not isinstance(c, str) or not _IDENT_SQL.match(c):
                        detalhes.append(
                            DetalheErro(
                                campo="nos",
                                mensagem=f"Write '{nid}': chave '{c}' is not a simple SQL identifier.",
                            )
                        )
        mapeamento = cfg.get("mapeamento")
        if mapeamento is not None and not isinstance(mapeamento, list):
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=f"Write '{nid}': mapeamento must be a list of {{de, para}}.",
                )
            )
        else:
            for m in mapeamento or []:
                para = m.get("para") if isinstance(m, dict) else None
                if not para or not isinstance(para, str) or not _IDENT_SQL.match(para):
                    detalhes.append(
                        DetalheErro(
                            campo="nos",
                            mensagem=f"Write '{nid}': mapeamento with invalid 'para' (SQL identifier).",
                        )
                    )
    elif modo == "sql":
        sql = cfg.get("sql")
        if not sql or not isinstance(sql, str) or not sql.strip():
            detalhes.append(
                DetalheErro(campo="nos", mensagem=f"Write '{nid}' (modo sql) requires config.sql.")
            )
        else:
            # A single DML statement. Strips the final terminator and the string literals (a ';'
            # inside 'text' is data, not a separator); anything left with ';' means multiple
            # statements, which is refused (anti-injection / writing outside the target). An empty
            # core is a degenerate statement.
            nucleo = sql.strip().rstrip(";").strip()
            if not nucleo:
                detalhes.append(
                    DetalheErro(
                        campo="nos", mensagem=f"Write '{nid}' (modo sql): empty DML statement."
                    )
                )
            elif ";" in _SQL_LITERAL.sub("", nucleo):
                detalhes.append(
                    DetalheErro(
                        campo="nos",
                        mensagem=f"Write '{nid}' (modo sql): only one statement (inner ';' not allowed).",
                    )
                )
    _validar_limpar_escopo(nid, cfg, definicao, detalhes)


def _parametros_vinculaveis(definicao: dict) -> dict[str, dict]:
    """Name the SQL BINDS -> declared parameter that produces it. `competencia` expands into
    `{name}_inicio`/`{name}_fim` (the raw name is not a bind); the others bind by their own name."""
    out: dict[str, dict] = {}
    for p in parametros_declarados(definicao):
        if not isinstance(p, dict) or not isinstance(p.get("nome"), str):
            continue
        if p.get("tipo") == "competencia":
            out[f"{p['nome']}_inicio"] = p
            out[f"{p['nome']}_fim"] = p
        else:
            out[p["nome"]] = p
    return out


def _validar_limpar_escopo(nid, cfg: dict, definicao: dict, detalhes: list[DetalheErro]) -> None:
    """`limpar_escopo`: {target column: ':parameter'} that the write deletes and rewrites in the
    same transaction. Each refusal here closes a way for the DELETE to erase the wrong thing.

    The parameter must be REQUIRED (a scope parameter is required): when blank, `WHERE col = NULL`
    deletes nothing and the write lands on top of the previous run, which is the defect the field
    exists to close, now silent. Not `segredo` either: the scope is data identity, and a
    credential does not identify a row."""
    if "limpar_escopo" not in cfg:
        return
    from ..conexoes.conectores.base import ESTRATEGIAS_ESCOPO, REF_PARAMETRO_ESCOPO

    def erro(msg: str) -> None:
        detalhes.append(DetalheErro(campo="nos", mensagem=f"Write '{nid}': limpar_escopo {msg}"))

    if cfg.get("modo") != "tabela":
        erro("only applies in modo 'tabela'.")
        return
    estrategia = cfg.get("estrategia", "append")
    if estrategia not in ESTRATEGIAS_ESCOPO:
        # truncate_insert already clears the WHOLE table: adding a scope would say two things.
        erro(
            f"does not combine with estrategia '{estrategia}' (use {' or '.join(ESTRATEGIAS_ESCOPO)})."
        )
    escopo = cfg["limpar_escopo"]
    if not isinstance(escopo, dict) or not escopo:
        erro("must be a non-empty object {column: ':parameter'}.")
        return
    mapeamento = cfg.get("mapeamento")
    gravadas = (
        {m.get("para") for m in mapeamento if isinstance(m, dict)}
        if isinstance(mapeamento, list) and mapeamento
        else None  # no mapeamento = same-named input columns: checked at assembly time (preview)
    )
    vinculaveis = _parametros_vinculaveis(definicao)
    competencias = {p["nome"] for p in vinculaveis.values() if p.get("tipo") == "competencia"}
    for coluna, ref in escopo.items():
        if not isinstance(coluna, str) or not _IDENT_SQL.match(coluna):
            erro(f"column '{coluna}' is not a simple SQL identifier.")
            continue
        if gravadas is not None and coluna not in gravadas:
            # A scope column the write does not store: the row enters without the scope value
            # and the next run's DELETE never reaches it.
            erro(f"column '{coluna}' is not written by the mapeamento.")
        m = REF_PARAMETRO_ESCOPO.match(ref) if isinstance(ref, str) else None
        if not m:
            erro(f"'{coluna}': value {ref!r} must be a parameter (':name').")
            continue
        nome = m.group(1)
        p = vinculaveis.get(nome)
        if p is None:
            dica = (
                f" (a period binds ':{nome}_inicio' or ':{nome}_fim')."
                if nome in competencias
                else "."
            )
            erro(f"'{coluna}': parameter '{nome}' is not declared{dica}")
            continue
        if p.get("segredo") is True:
            erro(
                f"'{coluna}': parameter '{p['nome']}' is a secret; a credential does not delimit a scope."
            )
        if p.get("obrigatorio") is not True:
            erro(
                f"'{coluna}': parameter '{p['nome']}' must be required; if blank, the "
                "scope cleanup would delete nothing and the write would land on top."
            )


def _validar_http(nid, cfg: dict, detalhes: list[DetalheErro]) -> None:
    """Validate an `http` node (effect). Connection/flag compatibility is checked at publish time."""
    if not cfg.get("conexao_id"):
        detalhes.append(
            DetalheErro(campo="nos", mensagem=f"HTTP '{nid}' requires config.conexao_id.")
        )
    metodo = cfg.get("metodo")
    if metodo not in _METODOS_HTTP:
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=f"HTTP '{nid}': invalid metodo '{metodo}' (use {list(_METODOS_HTTP)}).",
            )
        )
    modo = cfg.get("modo")
    if modo not in ("unico", "por_linha", "lote"):
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=f"HTTP '{nid}': invalid modo '{modo}' (use 'unico', 'por_linha' or 'lote').",
            )
        )
    caminho = cfg.get("caminho")
    # A RELATIVE path is required: starts with '/' and NOT with '//'. The final URL is built by
    # plain concatenation (connection base + path); '//host' would become a protocol-relative URL
    # and leak the token to another host, so it is refused here and never resolved by urljoin.
    if (
        not caminho
        or not isinstance(caminho, str)
        or not caminho.startswith("/")
        or caminho.startswith("//")
    ):
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=f"HTTP '{nid}': 'caminho' must be relative (start with '/', never '//').",
            )
        )
    # Body: `colunas` (default) | `template` | `coluna_json`. `coluna_json` requires `corpo_coluna`
    # (name of the column whose value is the raw JSON body).
    corpo_tipo = cfg.get("corpo_tipo")
    if corpo_tipo is not None and corpo_tipo not in ("colunas", "template", "coluna_json"):
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=f"HTTP '{nid}': invalid corpo_tipo '{corpo_tipo}' "
                "(use 'colunas', 'template' or 'coluna_json').",
            )
        )
    if corpo_tipo == "coluna_json":
        cc = cfg.get("corpo_coluna")
        if not cc or not isinstance(cc, str) or not cc.strip():
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=f"HTTP '{nid}': corpo_tipo 'coluna_json' requires config.corpo_coluna "
                    "(column name).",
                )
            )
    # Dynamic per-request headers: a list of {nome, valor} (canonical) or an object {nome: valor}.
    # The name cannot be empty; `valor` accepts {{col}}/:param placeholders (resolved per request).
    extra = cfg.get("headers_extra")
    if extra is not None:
        if isinstance(extra, dict):
            for nome in extra:
                if not nome or not isinstance(nome, str) or not nome.strip():
                    detalhes.append(
                        DetalheErro(
                            campo="nos",
                            mensagem=f"HTTP '{nid}': header name cannot be empty.",
                        )
                    )
        elif isinstance(extra, list):
            for h in extra:
                nome = h.get("nome") if isinstance(h, dict) else None
                if not nome or not isinstance(nome, str) or not nome.strip():
                    detalhes.append(
                        DetalheErro(
                            campo="nos",
                            mensagem=f"HTTP '{nid}': each extra header requires a non-empty 'nome'.",
                        )
                    )
        else:
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=f"HTTP '{nid}': 'headers_extra' must be a list of {{nome, valor}} or an object.",
                )
            )
    _faixa_inteiro(cfg, "concorrencia", 1, 16, nid, "HTTP", detalhes)
    _faixa_inteiro(cfg, "lote_tamanho", 1, 10000, nid, "HTTP", detalhes)
    _faixa_inteiro(cfg, "timeout_segundos", 1, 600, nid, "HTTP", detalhes)
    taxa = cfg.get("taxa_por_segundo")
    if taxa is not None and (
        not isinstance(taxa, (int, float)) or isinstance(taxa, bool) or taxa <= 0
    ):
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=f"HTTP '{nid}': 'taxa_por_segundo' must be greater than zero when present.",
            )
        )
    # Response extraction (por_linha/lote): each `{de: JSON path, para: column}` becomes a snapshot
    # column. Without it, using a response value required parsing `_resposta` in a later SQL node.
    campos = cfg.get("resposta_campos")
    if campos is not None:
        if not isinstance(campos, list):
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=f"HTTP '{nid}': 'resposta_campos' must be a list of {{de, para}}.",
                )
            )
        else:
            from ..execucao.no_http import COLUNAS_RESERVADAS

            vistos: set[str] = set()
            for c in campos:
                de = c.get("de") if isinstance(c, dict) else None
                para = c.get("para") if isinstance(c, dict) else None
                if not de or not isinstance(de, str) or not de.strip():
                    detalhes.append(
                        DetalheErro(
                            campo="nos",
                            mensagem=f"HTTP '{nid}': response field without 'de' (JSON path).",
                        )
                    )
                if not para or not isinstance(para, str) or not _IDENT_SQL.match(para):
                    detalhes.append(
                        DetalheErro(
                            campo="nos",
                            mensagem=f"HTTP '{nid}': response field with invalid 'para' "
                            "(simple SQL identifier).",
                        )
                    )
                    continue
                if para in COLUNAS_RESERVADAS:
                    detalhes.append(
                        DetalheErro(
                            campo="nos",
                            mensagem=f"HTTP '{nid}': '{para}' is a column reserved by the node "
                            f"(use another name; reserved: {list(COLUNAS_RESERVADAS)}).",
                        )
                    )
                elif para in vistos:
                    detalhes.append(
                        DetalheErro(
                            campo="nos",
                            mensagem=f"HTTP '{nid}': response column '{para}' is repeated.",
                        )
                    )
                vistos.add(para)
    if cfg.get("incluir_resposta_completa") is not None and not isinstance(
        cfg.get("incluir_resposta_completa"), bool
    ):
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=f"HTTP '{nid}': 'incluir_resposta_completa' must be true/false.",
            )
        )
    exemplo = cfg.get("resposta_exemplo")
    if exemplo is not None:
        if not isinstance(exemplo, str):
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=f"HTTP '{nid}': 'resposta_exemplo' must be JSON as text.",
                )
            )
        else:
            try:
                json.loads(exemplo)
            except (ValueError, TypeError):
                detalhes.append(
                    DetalheErro(
                        campo="nos",
                        mensagem=f"HTTP '{nid}': 'resposta_exemplo' is not parseable JSON.",
                    )
                )


def _validar_email(nid, cfg: dict, definicao: dict, detalhes: list[DetalheErro]) -> None:
    """Validate an `email` node (effect). Connection/flag compatibility is checked at publish time.

    Also checks that the e-mail text does NOT reference a `segredo: true` parameter: the body
    leaves the platform for a third party's inbox, and audit-trail redaction cannot reach what
    was already sent."""
    # Local import: the format/encoding/mode lists live in the module that IMPLEMENTS them (single
    # source), and the connector chain does not need to load just to validate a definition.
    from ..execucao.email_anexos import CODIFICACOES, CODIFICACOES_CONTEUDO, FORMATOS
    from ..execucao.email_template import placeholders
    from ..execucao.no_email import MODOS

    def erro(msg: str) -> None:
        """Node prefix in a single function: 20 identical `DetalheErro(campo="nos", ...)` hid
        the rule behind boilerplate."""
        detalhes.append(DetalheErro(campo="nos", mensagem=f"E-mail '{nid}': {msg}"))

    if not cfg.get("conexao_id"):
        erro("requires config.conexao_id.")
    modo = cfg.get("modo")
    if modo not in MODOS:
        erro(f"invalid modo '{modo}' (use {' or '.join(MODOS)}).")
    for campo in ("para", "assunto", "corpo"):
        v = cfg.get(campo)
        if not v or not isinstance(v, str) or not v.strip():
            erro(f"requires config.{campo}.")
    corpo_tipo = cfg.get("corpo_tipo")
    if corpo_tipo is not None and corpo_tipo not in ("html", "texto"):
        erro(f"invalid corpo_tipo '{corpo_tipo}' (use 'html' or 'texto').")
    tol = cfg.get("tolerancia_falha")
    if tol is not None and tol not in ("abortar", "continuar"):
        erro(f"invalid tolerancia_falha '{tol}' (use 'abortar' or 'continuar').")
    _faixa_inteiro(cfg, "tabela_limite", 1, 5000, nid, "E-mail", detalhes)
    _faixa_inteiro(cfg, "timeout_segundos", 1, 600, nid, "E-mail", detalhes)
    _faixa_inteiro(cfg, "tentativas", 1, 10, nid, "E-mail", detalhes)

    segredos = nomes_segredo(definicao)
    for campo in ("assunto", "corpo", "para", "cc", "cco"):
        texto = cfg.get(campo)
        if not isinstance(texto, str):
            continue
        for ph in placeholders(texto):
            nome_param = ph[len("param.") :] if ph.startswith("param.") else None
            if nome_param and nome_param in segredos:
                erro(
                    f"'{campo}' references the secret parameter '{nome_param}'; "
                    "a credential cannot go in an e-mail."
                )

    lista = cfg.get("anexos")
    if lista is not None and not isinstance(lista, list):
        erro("'anexos' must be a list.")
        return
    if lista is not None and len(lista) > _MAX_ANEXOS:
        erro(f"at most {_MAX_ANEXOS} attachments.")
    for anexo in lista or []:
        if not isinstance(anexo, dict):
            erro("an attachment must be an object.")
            continue
        origem = anexo.get("origem") or "dados"
        if origem not in ("dados", "coluna"):
            erro(f"attachment with invalid origem '{origem}' (use 'dados' or 'coluna').")
            continue
        nome = anexo.get("nome")
        tem_caminho = not isinstance(nome, str) or "/" in nome or "\\" in nome or ".." in nome
        if nome is not None and tem_caminho:
            erro("attachment name cannot contain a path ('/', '\\' or '..').")
        if origem == "dados":
            formato = anexo.get("formato") or "csv"
            if formato not in FORMATOS:
                erro(f"invalid attachment formato '{formato}' (use {list(FORMATOS)}).")
            delim = anexo.get("delimitador")
            if delim is not None and (not isinstance(delim, str) or len(delim) != 1):
                erro("attachment delimitador must be 1 character.")
            cod = anexo.get("codificacao")
            if cod is not None and cod not in CODIFICACOES:
                erro(f"invalid codificacao '{cod}' (use {list(CODIFICACOES)}).")
        else:
            if not anexo.get("coluna_conteudo"):
                erro("a column attachment requires 'coluna_conteudo'.")
            cod = anexo.get("codificacao_conteudo")
            if cod is not None and cod not in CODIFICACOES_CONTEUDO:
                erro(f"invalid codificacao_conteudo '{cod}' (use {list(CODIFICACOES_CONTEUDO)}).")


# Possible origins of the `fonte` node. Being an ALLOWLIST matters: otherwise `origem: "lake"` (or a
# typo) would slip silently through validation and only fail at runtime as `fonte_sem_conexao`, a
# message that sends you to debug the connection when the problem was the origin name.
# `lake` = dataset of the external INGESTION (read through the `ingestao_lotes` index).
# `publicado` = dataset that an `saida` node published to silver/gold (read through the POINTER).
# They are different things and therefore different origins: the first asks for `fonte_id`, the
# second for `dataset`, and an origin with mutually exclusive fields is the ambiguity that turns
# into surprising behavior.
# `objeto` = ONE file of a read-only layer, by path: there is no pointer or index there, the file
# is the address (`ingestao.objeto`).
ORIGENS_FONTE = ("conexao", "pipeline", "lake", "publicado", "objeto")


DESTINOS_SAIDA = ("arquivo", "lake", "iceberg")
# Layers a pipeline PUBLISHES. The evidence ones (landing/bronze/quarantine) belong to ingestion
# and are immutable: an output node pointing there would erase the proof of origin.
CAMADAS_SAIDA = ("silver", "gold")
# In Iceberg the dataset is a table name in the catalog (other engines read the same one): no hyphen.
_SLUG_ICEBERG = re.compile(r"^[a-z0-9_]+$")
# Origins of a partition segment. ALLOWLIST for the same reason as `ORIGENS_FONTE`:
# `origem: "parametros"` (a typo) would pass silently and only show up in the bucket, weeks later,
# as a dataset that never got any scope.
ORIGENS_PARTICAO = ("parametro", "coluna")
# The key is the `key=` of the hive path. A closed alphabet because it is assembled as text in the
# object prefix and read by query engines as a partition column name.
_CHAVE_PARTICAO = re.compile(r"^[a-z0-9_]+$")


def _validar_particao(
    nid: str, cfg: dict, definicao: dict, destino: str, detalhes: list[DetalheErro]
) -> None:
    """Validate `config.particao` of the `saida` node: the hive segments that characterize the
    publication (`<dataset>/<key>=<value>/...`).

    Called BEFORE the per-destination branch, for the same reason as `camada`: the `arquivo` path
    returns elsewhere, and a partition declared there would be accepted silently with no effect;
    the node would seem to publish per day/unit and would publish a single file."""
    particao = cfg.get("particao")
    if particao is None:
        return
    if destino != "lake":
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=f"Output '{nid}': config.particao only applies to destino 'lake'.",
            )
        )
        return
    if not isinstance(particao, list) or not particao:
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=f"Output '{nid}': config.particao must be a non-empty list.",
            )
        )
        return

    declarados = {p.get("nome") for p in parametros_declarados(definicao) if isinstance(p, dict)}
    segredos = nomes_segredo(definicao)
    chaves: list[str] = []
    for item in particao:
        if not isinstance(item, dict):
            detalhes.append(
                DetalheErro(
                    campo="nos", mensagem=f"Output '{nid}': each partition must be an object."
                )
            )
            continue
        origem = item.get("origem")
        nome = item.get("nome")
        if origem not in ORIGENS_PARTICAO:
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=(
                        f"Output '{nid}': partition with origem '{origem}' does not exist "
                        f"(use {', '.join(ORIGENS_PARTICAO)})."
                    ),
                )
            )
            continue
        if not nome or not isinstance(nome, str) or not nome.strip():
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=f"Output '{nid}': partition without 'nome' (parameter or column).",
                )
            )
            continue
        if origem == "parametro" and nome not in declarados:
            # Without this, EVERY run would publish to the same empty `<key>=` path, and the error
            # would only show up months later, in the bucket, far from whoever wrote the definition.
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=(
                        f"Output '{nid}': partition by parameter '{nome}', which the pipeline does not "
                        "declare in 'parametros'."
                    ),
                )
            )
        elif origem == "parametro" and nome in segredos:
            # The value becomes a PATH in the bucket, and a path cannot be redacted afterwards: the
            # credential would stay in the object name, readable by anyone who can only list.
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=(
                        f"Output '{nid}': parameter '{nome}' is a secret and cannot become a path "
                        "in the lake."
                    ),
                )
            )
        chave = item.get("chave") or nome.lower()
        if not isinstance(chave, str) or not _CHAVE_PARTICAO.match(chave):
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=(
                        f"Output '{nid}': invalid partition key '{chave}' "
                        "(lower-case, digits and '_'); set 'chave' when the name does not fit."
                    ),
                )
            )
        elif chave in chaves:
            # Repeated, it would become `project_id=1/project_id=2`: a path no hive reader resolves,
            # and it would hide the second scope inside the first.
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=f"Output '{nid}': partition key '{chave}' is repeated.",
                )
            )
        else:
            chaves.append(chave)


def _validar_saida(nid: str, cfg: dict, definicao: dict, detalhes: list[DetalheErro]) -> None:
    """Validate the `saida` node per destination. `arquivo` is the usual one (`nome` slug + format);
    `lake` publishes to a derived layer (`silver` by default, or `gold`), where the dataset name IS
    the path in the bucket."""
    destino = str(cfg.get("destino") or "arquivo").lower()
    if destino not in DESTINOS_SAIDA:
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=(
                    f"Output '{nid}': destino '{destino}' does not exist "
                    f"(use {', '.join(DESTINOS_SAIDA)})."
                ),
            )
        )
        return

    # Checked BEFORE the per-destination branch: the `arquivo` path returns elsewhere and would
    # never get here, so `camada` on a non-`lake` destination would pass silently, making the node
    # look like it publishes to gold while publishing elsewhere. In `iceberg` the layer is the catalog.
    camada = cfg.get("camada")
    if camada is not None and (destino != "lake" or camada not in CAMADAS_SAIDA):
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=(
                    f"Output '{nid}': config.camada only applies to destino 'lake' and "
                    f"accepts {', '.join(CAMADAS_SAIDA)}."
                ),
            )
        )
    _validar_particao(nid, cfg, definicao, destino, detalhes)

    if destino in ("lake", "iceberg"):
        dataset = cfg.get("dataset")
        # The `/` only applies to the lake: in Iceberg the dataset becomes a TABLE NAME (see below).
        padrao = _SLUG_LAKE if destino == "lake" else _SLUG
        if not dataset or not isinstance(dataset, str) or not padrao.match(dataset):
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=(
                        f"Output '{nid}' (destino '{destino}') requires config.dataset "
                        "(slug: letters, digits, '_' or '-'"
                        + (
                            "; '/' groups by family, up to 3 levels)."
                            if destino == "lake"
                            else ")."
                        )
                    ),
                )
            )
        if destino == "iceberg" and dataset and not _SLUG_ICEBERG.match(dataset):
            # In Iceberg the dataset becomes a TABLE NAME in the catalog, which other engines also
            # read. A hyphen would force quoting the identifier in every query.
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=(
                        f"Output '{nid}' (destino 'iceberg'): the dataset becomes a table name; "
                        "use only lower-case letters, digits and '_' (no hyphen)."
                    ),
                )
            )
        if cfg.get("formato") == "csv":
            # The layer is consumed by analytical tools: CSV loses types and inflates the object.
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=f"Output '{nid}': the lake layer is always Parquet, not CSV.",
                )
            )
        if cfg.get("caminho"):
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=(
                        f"Output '{nid}': with destino '{destino}' the path belongs to the bucket "
                        "and is not configurable."
                    ),
                )
            )
        return

    nome = cfg.get("nome")
    if not nome or not isinstance(nome, str) or not _SLUG.match(nome):
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=f"Output '{nid}' requires config.nome (slug: letters, digits, '_' or '-').",
            )
        )
    fmt = cfg.get("formato")
    if fmt is not None and fmt not in ("parquet", "csv"):
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=f"Output '{nid}': formato '{fmt}' is not supported (use 'parquet' or 'csv').",
            )
        )


def _validar_extracao_paralela(nid: str, cfg: dict, detalhes: list[DetalheErro]) -> None:
    """Validate `config.extracao_paralela`: extracting the source in processes.

    Only makes sense with origin `conexao` (the others read parquet from the lake, where there is
    no driver to parallelize), and requires `coluna`: a STABLE key of the origin, because the read
    is `(nolock)` and a row that changed slice between two scans would come out duplicated or
    missing. There is no way for the code to discover which column the origin does not update, so
    it is declared, and a malformed config is refused instead of ignored."""
    par = cfg.get("extracao_paralela")
    if par is None:
        return
    rotulo = f"Source '{nid}': config.extracao_paralela"
    if not isinstance(par, dict):
        detalhes.append(DetalheErro(campo="nos", mensagem=f"{rotulo} must be an object."))
        return
    if (cfg.get("origem") or "conexao") != "conexao":
        detalhes.append(
            DetalheErro(campo="nos", mensagem=f"{rotulo} only applies to origem 'conexao'.")
        )
    coluna = par.get("coluna")
    if not isinstance(coluna, str) or not _IDENT_SQL.match(coluna):
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=f"{rotulo}.coluna must be a simple SQL identifier (a stable key of the origin).",
            )
        )
    n = par.get("processos")
    if n is not None and (not isinstance(n, int) or isinstance(n, bool) or n < 2):
        detalhes.append(
            DetalheErro(campo="nos", mensagem=f"{rotulo}.processos must be an integer >= 2.")
        )
    extras = set(par) - {"coluna", "processos"}
    if extras:
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=f"{rotulo}: unknown field {sorted(extras)}; accepted silently it would configure nothing.",
            )
        )


def _validar_fonte(nid: str, cfg: dict, detalhes: list[DetalheErro]) -> None:
    """Validate the `fonte` node per origin. Each origin requires its own fields and refuses the
    others': a config half of one mode and half of another is ambiguity that turns into surprising
    behavior."""
    _validar_extracao_paralela(nid, cfg, detalhes)
    origem = cfg.get("origem") or "conexao"
    if origem not in ORIGENS_FONTE:
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=(
                    f"Source '{nid}': origem '{origem}' does not exist (use {', '.join(ORIGENS_FONTE)})."
                ),
            )
        )
        return

    if origem == "pipeline":
        if not cfg.get("pipeline_id") or not cfg.get("no_id"):
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=f"Source '{nid}' (origem 'pipeline') requires config.pipeline_id and config.no_id.",
                )
            )
        if cfg.get("conexao_id"):
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=f"Source '{nid}': use origem 'pipeline' OR conexao_id, not both.",
                )
            )
    elif origem == "publicado":
        # The SAME rule as the node that publishes: a `ref//05` or `../x` here would only fail on
        # read, and the symptom would be "dataset not published": the right error, hidden cause.
        ds_pub = cfg.get("dataset")
        if not ds_pub or not isinstance(ds_pub, str) or not _SLUG_LAKE.match(ds_pub):
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=(
                        f"Source '{nid}' (origem 'publicado') requires config.dataset, the published "
                        "dataset to read (slug; '/' groups by family, up to 3 levels)."
                    ),
                )
            )
        camada = cfg.get("camada") or "gold"
        if camada not in ("silver", "gold"):
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=(
                        f"Source '{nid}': config.camada accepts 'silver' or 'gold' (what an "
                        "`saida` node publishes)."
                    ),
                )
            )
        if cfg.get("pipeline_id") or cfg.get("fonte_id"):
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=(
                        f"Source '{nid}': origem 'publicado' reads the ARTIFACT through the pointer; do not use "
                        "pipeline_id (that would re-run the producer) or fonte_id."
                    ),
                )
            )
        particao = cfg.get("particao")
        if particao is not None and (
            not isinstance(particao, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in particao.items())
        ):
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=(
                        f"Source '{nid}': config.particao is a key->value map "
                        "(the value accepts ':parameter')."
                    ),
                )
            )
        # `opcional` only counts as a boolean: a text "true" would pass silently and the source would
        # bring the chain down again on the first missing scope, which is what the flag avoids.
        if "opcional" in cfg and not isinstance(cfg["opcional"], bool):
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=(
                        f"Source '{nid}': config.opcional is true or false; with true, a scope never "
                        "published becomes an empty table instead of failing the execution."
                    ),
                )
            )
        colunas = cfg.get("colunas")
        if colunas is not None and (
            not isinstance(colunas, list)
            or not colunas
            or not all(isinstance(c, str) and _IDENT_SQL.match(c) for c in colunas)
            or cfg.get("opcional") is not True
        ):
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=(
                        f"Source '{nid}': config.colunas (a list of column names) only applies with "
                        "opcional: true; it is the schema of the empty table when the dataset was "
                        "never published."
                    ),
                )
            )
    elif origem == "objeto":
        from ..ingestao.objeto import problemas_da_config

        for problema in problemas_da_config(cfg):
            detalhes.append(
                DetalheErro(campo="nos", mensagem=f"Source '{nid}' (origem 'objeto'): {problema}")
            )
    elif origem == "lake":
        if not cfg.get("fonte_id"):
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=(
                        f"Source '{nid}' (origem 'lake') requires config.fonte_id, the ingestion "
                        "dataset to read."
                    ),
                )
            )
        if cfg.get("conexao_id") or cfg.get("pipeline_id"):
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=(
                        f"Source '{nid}': origem 'lake' reads through the ingestion index; do not use "
                        "conexao_id or pipeline_id."
                    ),
                )
            )
        for campo in ("chave_min", "chave_max"):
            valor = cfg.get(campo)
            if valor is not None and not isinstance(valor, str):
                detalhes.append(
                    DetalheErro(
                        campo="nos",
                        mensagem=f"Source '{nid}': config.{campo} must be text (a slice key).",
                    )
                )


def _validar_ingestao(nid: str, cfg: dict, definicao: dict, detalhes: list[DetalheErro]) -> None:
    """Validate the `ingestao` node: brings public external data into the lake (landing -> bronze).

    The config is DELIBERATELY short: `fonte_id` and, at most, a window. URL, format, encoding,
    expected schema and cadence live in the source's declarative contract
    (`ingestao_fontes.contrato`), maintained by an admin outside the Studio. Letting the author
    retype the URL in the node would create two truths about the same origin, and the graph's
    would silently win."""
    if not cfg.get("fonte_id"):
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=(
                    f"Ingestion '{nid}' requires config.fonte_id, the registered external source to "
                    "reload."
                ),
            )
        )
    # The node fetches from the EXTERNAL ORIGIN: there is no input data it could use. Wiring an edge
    # into it looks like chaining and is not: the predecessor's data would be silently ignored.
    # Checked here (and not only in the engine) so the error shows up on save, not mid-execution.
    if any(a.get("para") == nid for a in definicao.get("arestas", [])):
        detalhes.append(
            DetalheErro(
                campo="arestas",
                mensagem=(
                    f"Ingestion '{nid}' does not accept input: it fetches from the external origin. To "
                    "chain pipelines, use a source node with origem 'pipeline' in the next pipeline."
                ),
            )
        )
    # Fields that belong to the source contract, refused here instead of ignored: a config accepted
    # silently with no effect is the worst kind, it looks configured and configures nothing.
    for proibido in ("url", "contrato", "classe", "formato", "conexao_id"):
        if cfg.get(proibido) is not None:
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=(
                        f"Ingestion '{nid}': '{proibido}' belongs to the source contract, not to the node. "
                        "Edit the ingestion source (admin) instead of the graph."
                    ),
                )
            )
    # Explicit window: ISO dates only. A `:name` parameter is left out on purpose: the window decides
    # HOW MANY requests go to the public origin, and a blank parameter ("blank = everything") would
    # turn into a ten-year backfill fired by accident.
    for campo in ("inicio", "fim"):
        valor = cfg.get(campo)
        if valor is None:
            continue
        if not isinstance(valor, str) or not _data_iso(valor):
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=(
                        f"Ingestion '{nid}': config.{campo} must be an ISO date (YYYY-MM-DD); "
                        "without a window, the reload is incremental (from the last point to today)."
                    ),
                )
            )
    ini, fim = cfg.get("inicio"), cfg.get("fim")
    if isinstance(ini, str) and isinstance(fim, str) and _data_iso(ini) and _data_iso(fim):
        if _data_iso(ini) > _data_iso(fim):  # type: ignore[operator]
            detalhes.append(
                DetalheErro(
                    campo="nos",
                    mensagem=f"Ingestion '{nid}': the window start date is after the end date.",
                )
            )


def _data_iso(valor: str):
    from datetime import date as _d

    try:
        return _d.fromisoformat(valor)
    except (TypeError, ValueError):
        return None


def montante_publica(definicao: dict) -> bool:
    """Does the upstream of a chained trigger whose ROOT is this definition publish? Absent = yes.

    `"montante_publica": false` exists for a root that reads a producer through
    `origem: "pipeline"` at an as-of that is not the producer's own: chaining re-runs the upstream,
    and each output of it would move a pointer that ANOTHER consumer reads as frozen. With the
    flag, the upstream computes and stores a snapshot (what the root reads through the freshness
    map) and publishes nothing. Only the literal `false` turns it off; validation refuses
    anything that is not a bool."""
    return not (isinstance(definicao, dict) and definicao.get("montante_publica") is False)


def _validar_montante_publica(definicao: dict, detalhes: list[DetalheErro]) -> None:
    """`"false"` is truthy in any `if`, and `0`/`1` would pass as bool in a loose test: the flag
    decides whether a whole run publishes, so only the JSON `true`/`false` count."""
    if "montante_publica" in definicao and not isinstance(definicao["montante_publica"], bool):
        detalhes.append(
            DetalheErro(campo="montante_publica", mensagem='"montante_publica" must be true/false.')
        )


def _validar_grupo_serial(nid: str, no: dict, detalhes: list[DetalheErro]) -> None:
    """Validate `config.grupo_serial` (serial chain per group, `execucao.paralelo`). Only applies to
    a node that enters the serial queue: an external effect or an output outside the lake. On any
    other the field would do nothing, and a config accepted silently looks configured. Must be
    non-empty text: the group is the queue NAME, and `["b1"]` would become a queue called
    `"['b1']"`."""
    cfg = no.get("config") or {}
    if "grupo_serial" not in cfg:
        return
    g = cfg.get("grupo_serial")
    if not isinstance(g, str) or not g.strip():
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=f"Node '{nid}': config.grupo_serial must be non-empty text (the queue name).",
            )
        )
        return
    if not _no_serial(no):
        detalhes.append(
            DetalheErro(
                campo="nos",
                mensagem=(
                    f"Node '{nid}': config.grupo_serial only applies to an effect node (escrita_sql, http, "
                    "email, ingestao) or an output outside the lake; here it would do nothing."
                ),
            )
        )


def validar(definicao: dict) -> None:
    """Validate the structure; raises ErroAplicacao(422) with details if invalid."""
    if not isinstance(definicao, dict):
        raise _erro([DetalheErro(campo="definicao", mensagem="Invalid format.")])
    nos = definicao.get("nos", [])
    arestas = definicao.get("arestas", [])
    detalhes: list[DetalheErro] = []

    _validar_parametros(definicao, detalhes)
    _validar_bibliotecas(definicao, detalhes)
    _validar_montante_publica(definicao, detalhes)

    ids: list[str] = []
    for no in nos:
        nid = no.get("id")
        if not nid:
            detalhes.append(DetalheErro(campo="nos", mensagem="Node without id."))
        else:
            ids.append(nid)
        cfg = no.get("config") or {}
        _validar_grupo_serial(nid, no, detalhes)
        if no.get("tipo") == "fonte":
            _validar_fonte(nid, cfg, detalhes)
        # Comparison node (diff by key): requires config.chave (a non-empty list).
        if no.get("tipo") == "comparacao":
            ch = cfg.get("chave")
            if not ch or not isinstance(ch, list):
                detalhes.append(
                    DetalheErro(
                        campo="nos",
                        mensagem=f"Comparison '{nid}' requires config.chave (a list of columns).",
                    )
                )
        # Code node (procedural, sandboxed): requires config.codigo (non-empty source). Language
        # defaults to "python" (the only one supported).
        if no.get("tipo") == "python":
            cod = cfg.get("codigo")
            if not cod or not isinstance(cod, str) or not cod.strip():
                detalhes.append(
                    DetalheErro(
                        campo="nos",
                        mensagem=f"Code node '{nid}' requires config.codigo (non-empty source).",
                    )
                )
            ling = cfg.get("linguagem", "python")
            if ling != "python":
                detalhes.append(
                    DetalheErro(
                        campo="nos",
                        mensagem=(
                            f"Node '{nid}': linguagem '{ling}' is not supported (use 'python')."
                        ),
                    )
                )
        # Output node (publication): `arquivo` (default, export_dir) or `lake` (silver layer).
        if no.get("tipo") == "saida":
            _validar_saida(nid, cfg, definicao, detalhes)
        # EFFECT nodes: escrita_sql (writes to Postgres/SQL Server) and http (calls an API). The
        # structure is validated here; the connection's `permitir_escrita` flag is checked at publish.
        if no.get("tipo") == "escrita_sql":
            _validar_escrita_sql(nid, cfg, definicao, detalhes)
        if no.get("tipo") == "http":
            _validar_http(nid, cfg, detalhes)
        # `email` effect node: sends e-mail (template + attachments built from the data).
        if no.get("tipo") == "email":
            _validar_email(nid, cfg, definicao, detalhes)
        # `ingestao` effect node: brings external public data into the lake (bronze).
        if no.get("tipo") == "ingestao":
            _validar_ingestao(nid, cfg, definicao, detalhes)
    if len(set(ids)) != len(ids):
        detalhes.append(DetalheErro(campo="nos", mensagem="Duplicate node ids."))
    id_set = set(ids)

    # broken references
    for a in arestas:
        if a.get("de") not in id_set or a.get("para") not in id_set:
            detalhes.append(
                DetalheErro(campo="arestas", mensagem=f"Edge references a nonexistent node: {a}.")
            )

    if detalhes:
        raise _erro(detalhes)

    # cycle (DFS), only if the references are intact
    adj: dict[str, list[str]] = {i: [] for i in id_set}
    for a in arestas:
        adj[a["de"]].append(a["para"])
    estado: dict[str, int] = {}  # 0=visitando, 1=ok

    def tem_ciclo(n: str) -> bool:
        estado[n] = 0
        for viz in adj[n]:
            if estado.get(viz) == 0:
                return True
            if viz not in estado and tem_ciclo(viz):
                return True
        estado[n] = 1
        return False

    if any(n not in estado and tem_ciclo(n) for n in id_set):
        detalhes.append(DetalheErro(campo="arestas", mensagem="The graph has a cycle."))

    # disconnected node (only matters with more than 1 node)
    if len(id_set) > 1:
        conectados = {a["de"] for a in arestas} | {a["para"] for a in arestas}
        soltos = id_set - conectados
        if soltos:
            detalhes.append(
                DetalheErro(campo="nos", mensagem=f"Disconnected node(s): {sorted(soltos)}.")
            )

    if detalhes:
        raise _erro(detalhes)


def ordem_topologica(definicao: dict) -> list[str]:
    """Topological order of the ids (assumes it is already validated, with no cycle)."""
    nos = [n["id"] for n in definicao.get("nos", [])]
    arestas = definicao.get("arestas", [])
    grau = {i: 0 for i in nos}
    adj: dict[str, list[str]] = {i: [] for i in nos}
    for a in arestas:
        adj[a["de"]].append(a["para"])
        grau[a["para"]] += 1
    fila = [i for i in nos if grau[i] == 0]
    ordem = []
    while fila:
        n = fila.pop(0)
        ordem.append(n)
        for viz in adj[n]:
            grau[viz] -= 1
            if grau[viz] == 0:
                fila.append(viz)
    return ordem
