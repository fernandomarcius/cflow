"""Report layout: the declarative JSON this layer owns -- and only it.

The layout chooses and orders the columns of a node's snapshot, renames the header, applies CELL
formats, names the sheet and stamps it. What it CANNOT contain is an explicit refusal rather than
an "ignored field": SQL, formula, aggregation, total, `/100`, `null -> 0`. A field silently
accepted with no effect looks like configuration and is not -- and a `total` that "works" today
becomes the spreadsheet-side subtotal logic this layer exists to keep out.

Format is a CLOSED vocabulary, because each entry is an Excel `number_format` and never a
conversion: `percentual` (`0.00%`) is for fractions and `percentual_pontos` (`0.00"%"`) for values
that are already in points -- the node stores 12.34, the screen shows 12.34 and the cell shows
12.34. Dividing by 100 in the sheet would be a transformation; the literal suffix shows the same
number.
"""

from __future__ import annotations

import re
import uuid

from ..errors import ErroAplicacao

# declared format -> Excel number_format. Nothing here changes the cell VALUE.
FORMATOS: dict[str, str] = {
    "moeda": '"R$ "#,##0.00',
    # No space after the currency symbol. The vocabulary is closed, so this needs its own entry:
    # reusing `moeda` would change the cell of every other layout.
    "moeda_colada": '"R$"#,##0.00',
    "data": "dd/mm/yyyy",
    "percentual": "0.00%",
    "percentual_pontos": '0.00"%"',
    # Number with thousands separator and two decimals, WITHOUT a currency symbol. Own entry for
    # the same reason as `moeda_colada`: reusing `moeda` would put a symbol on a cell that has none.
    "decimal": "#,##0.00",
    # Percentage WITHOUT decimal places (`#%`): decimals would be noise for values such as 100 %
    # or 65 %. Distinct from `percentual` (`0.00%`).
    "percentual_inteiro": "#%",
    "inteiro": "0",
    "texto": "@",
}

# Top-level keys the layout accepts. Any other is refused (see `_PROIBIDAS` for the ones that have
# their own message).
_CHAVES = {
    "tipo",
    "nome",
    "descricao",
    "fonte",
    "aba",
    "colunas",
    "cabecalho_fixo",
    "autofiltro",
    "carimbo",
    "padrao",
    # Two header rows and trailing empty columns, for files that must keep that shape.
    "linhas_cabecalho",
    "colunas_vazias_no_fim",
    # Fixed block (a NAMED exception, see `_validar_bloco_fixo`): the file is not a plain table.
    # It has a merged title, loose blocks of values and a totals row ABOVE the data, with a live
    # `=SUM`.
    "titulo",
    "linhas_fixas",
    "totais",
    "primeira_linha_dado",
    "linha_cabecalho",
    "fonte_tamanho",
}
_CHAVES_COLUNA = {"coluna", "rotulo", "rotulo2", "formato", "largura", "negrito"}
# What the layer does NOT do. Each name here is a transformation disguised as formatting.
_PROIBIDAS = {
    "sql",
    "consulta",
    "formula",
    "formulas",
    "expressao",
    "total",
    "linha_total",
    "subtotal",
    "agregacao",
    "agregacoes",
    "agrupar",
    "filtro",
    "filtros",
    "where",
    "join",
    "juntar",
    "de_para",
    "depara",
    "mapeamento",
    "dividir",
    "escala",
    "nulo_para",
    "null_para",
    "valor_padrao",
    "calculado",
    "calculo",
}
# GROUPED layout: ONE file with the sheets of SEVERAL single-source layouts. It has no columns,
# formats or source of its own: each sheet IS an already published single-source layout, under the
# name the grouped layout gives it. This guarantees the same sheet, tab by tab, as each source's own
# file -- there is no second place describing the same presentation.
TIPO_SIMPLES = "simples"
TIPO_AGRUPADO = "agrupado"
_CHAVES_AGRUPADO = {"tipo", "nome", "descricao", "abas", "carimbo", "padrao"}
_CHAVES_ABA_AGRUPADA = {"aba", "layout"}
_RE_NOME_ABA_PROIBIDO = re.compile(r"[\[\]:*?/\\]")
TETO_LINHAS_EXCEL = 1_048_576  # rows per sheet, INCLUDING the header
ABA_CARIMBO = "Carimbo"


def _erro(codigo: str, mensagem: str) -> ErroAplicacao:
    return ErroAplicacao(codigo=codigo, mensagem=mensagem, status=400)


def _recusar_transformacao(chave: str, onde: str) -> None:
    raise _erro(
        "layout_transforma_dado",
        f"The layout does not transform data: '{chave}' in {onde} is not allowed. The report "
        "layer only chooses and orders columns, renames the header, applies cell formats, "
        "names the sheet and stamps it. Rules, filters, totals and calculations belong in a "
        "pipeline node.",
    )


def _texto_e_formula(valor: object) -> bool:
    """Excel interprets a cell starting with `=` (or `+`/`-`/`@` followed by a name) as a formula.
    Such a label would become a live formula in the header -- the only formula a layout could
    inject."""
    if not isinstance(valor, str):
        return False
    t = valor.strip()
    return t.startswith("=") or bool(re.match(r"^[+\-@][A-Za-z(]", t))


def nome_aba(titulo: str | None, padrao: str = "Dados") -> str:
    """Sanitizes a sheet name (<=31 chars, without `[]:*?/\\`). An empty result falls back to the
    default."""
    bruto = (titulo or padrao).strip() or padrao
    limpo = _RE_NOME_ABA_PROIBIDO.sub(" ", bruto).strip()
    return (limpo or padrao)[:31]


def validar(definicao: dict) -> None:
    """STRUCTURAL validation of the layout (independent of the snapshot). Raises `ErroAplicacao`.

    Checks: known keys (and a named refusal for the ones that transform data), `fonte` with a
    pipeline (uuid) and a node, at least one column, no repeated columns, format within the closed
    vocabulary, label/sheet that are not formulas. Whether the column exists in the snapshot is
    checked by `validar_contra_snapshot`, which needs the Parquet."""
    if not isinstance(definicao, dict):
        raise _erro("layout_invalido", "The layout definition must be a JSON object.")
    tipo = definicao.get("tipo", TIPO_SIMPLES)
    if tipo == TIPO_AGRUPADO:
        _validar_agrupado(definicao)
        return
    if tipo != TIPO_SIMPLES:
        raise _erro(
            "layout_tipo_invalido",
            f"'tipo' accepts '{TIPO_SIMPLES}' (the default) or '{TIPO_AGRUPADO}', not '{tipo}'.",
        )

    for chave in definicao:
        if chave in _PROIBIDAS:
            _recusar_transformacao(chave, "o layout")
        if chave not in _CHAVES:
            raise _erro(
                "layout_chave_desconhecida",
                f"Field '{chave}' is not part of the layout. Accepted: {', '.join(sorted(_CHAVES))}.",
            )

    fonte = definicao.get("fonte")
    if not isinstance(fonte, dict) or not fonte.get("pipeline") or not fonte.get("no"):
        raise _erro(
            "layout_fonte_invalida",
            "Provide 'fonte' with 'pipeline' (the pipeline id) and 'no' (the id of the node whose "
            "snapshot the report organizes).",
        )
    for chave in fonte:
        if chave in _PROIBIDAS:
            _recusar_transformacao(chave, "'fonte'")
        if chave not in {"pipeline", "no"}:
            raise _erro(
                "layout_fonte_invalida", f"Field '{chave}' is not part of 'fonte' (pipeline, no)."
            )
    try:
        uuid.UUID(str(fonte["pipeline"]))
    except ValueError:
        raise _erro(
            "layout_fonte_invalida", "'fonte.pipeline' must be the pipeline id (uuid)."
        ) from None
    if not isinstance(fonte["no"], str) or not fonte["no"].strip():
        raise _erro("layout_fonte_invalida", "'fonte.no' must be the node id.")

    aba = definicao.get("aba")
    if aba is not None and not isinstance(aba, str):
        raise _erro("layout_aba_invalida", "'aba' must be text.")
    if _texto_e_formula(aba):
        _recusar_transformacao("formula in the sheet name", "'aba'")

    colunas = definicao.get("colunas")
    if not isinstance(colunas, list) or not colunas:
        raise _erro("layout_sem_colunas", "The layout needs at least one column.")
    vistas: set[str] = set()
    for i, col in enumerate(colunas, start=1):
        if not isinstance(col, dict):
            raise _erro("layout_coluna_invalida", f"Column {i}: must be an object.")
        for chave in col:
            if chave in _PROIBIDAS:
                _recusar_transformacao(chave, f"column {i}")
            if chave not in _CHAVES_COLUNA:
                raise _erro(
                    "layout_coluna_invalida",
                    f"Column {i}: field '{chave}' is not allowed (coluna, rotulo, formato, largura).",
                )
        nome = col.get("coluna")
        if not isinstance(nome, str) or not nome.strip():
            raise _erro(
                "layout_coluna_invalida",
                f"Column {i}: provide 'coluna' (the name in the snapshot).",
            )
        if _texto_e_formula(nome):
            _recusar_transformacao("formula", f"column {i}")
        if nome in vistas:
            raise _erro(
                "layout_coluna_repetida", f"Column '{nome}' appears more than once in the layout."
            )
        vistas.add(nome)
        rotulo = col.get("rotulo")
        if rotulo is not None and not isinstance(rotulo, str):
            raise _erro("layout_coluna_invalida", f"Column '{nome}': 'rotulo' must be text.")
        if _texto_e_formula(rotulo):
            _recusar_transformacao("formula in the label", f"column '{nome}'")
        formato = col.get("formato")
        if formato is not None and formato not in FORMATOS:
            raise _erro(
                "layout_formato_invalido",
                f"Column '{nome}': format '{formato}' does not exist. Use one of: "
                f"{', '.join(FORMATOS)}. A format applies to the cell, never converts the value.",
            )
        largura = col.get("largura")
        if largura is not None and (
            not isinstance(largura, int | float) or isinstance(largura, bool) or largura <= 0
        ):
            raise _erro("layout_coluna_invalida", f"Column '{nome}': 'largura' must be > 0.")

    _validar_bloco_fixo(definicao)

    linhas = definicao.get("linhas_cabecalho", 1)
    if linhas not in (1, 2):
        raise _erro("layout_invalido", "'linhas_cabecalho' accepts 1 or 2.")
    if linhas == 1 and any(c.get("rotulo2") for c in definicao.get("colunas", [])):
        # A `rotulo2` that is never written looks configured and does nothing -- the same reason
        # unknown keys are refused.
        raise _erro("layout_invalido", "'rotulo2' requires 'linhas_cabecalho': 2.")
    vazias = definicao.get("colunas_vazias_no_fim", 0)
    if not isinstance(vazias, int) or isinstance(vazias, bool) or vazias < 0:
        raise _erro("layout_invalido", "'colunas_vazias_no_fim' must be an integer >= 0.")

    for flag in ("cabecalho_fixo", "autofiltro", "carimbo", "padrao"):
        if flag in definicao and not isinstance(definicao[flag], bool):
            raise _erro("layout_invalido", f"'{flag}' must be true or false.")
    for campo in ("nome", "descricao"):
        if campo in definicao and definicao[campo] is not None:
            if not isinstance(definicao[campo], str):
                raise _erro("layout_invalido", f"'{campo}' must be text.")
            if _texto_e_formula(definicao[campo]):
                _recusar_transformacao("formula", f"'{campo}'")


def eh_agrupado(definicao: object) -> bool:
    return isinstance(definicao, dict) and definicao.get("tipo") == TIPO_AGRUPADO


def abas_do_agrupado(definicao: dict) -> list[tuple[str, str]]:
    """`[(sheet name, single-source layout id)]`, in the order of the file's sheets."""
    return [(a["aba"], str(a["layout"])) for a in definicao.get("abas", [])]


def _validar_agrupado(definicao: dict) -> None:
    """Structure of the grouped layout. What the layer refuses stays refused -- and there is nowhere
    to put a column, format or rule here: each sheet is organized by the single-source layout it
    points to. A single-source key (`colunas`, `fonte`, `aba`...) in a grouped layout is an unknown
    key: accepting it would look like configuration and do nothing.

    Checks: at least one sheet; each sheet with `aba` (name) and `layout` (uuid); a sheet name Excel
    accepts as is (<=31 chars, without `[]:*?/\\`, no formula) and unique ignoring case -- also
    against `Carimbo`; the same layout does not appear twice. That the referenced layout exists, is
    published and is a single-source one is checked in the service, which has the database."""
    for chave in definicao:
        if chave in _PROIBIDAS:
            _recusar_transformacao(chave, "the grouped layout")
        if chave not in _CHAVES_AGRUPADO:
            raise _erro(
                "layout_chave_desconhecida",
                f"Field '{chave}' is not part of the grouped layout. Accepted: "
                f"{', '.join(sorted(_CHAVES_AGRUPADO))}. The columns, formats and sheet of each "
                "part belong to the single-source layout the sheet points to.",
            )
    abas = definicao.get("abas")
    if not isinstance(abas, list) or not abas:
        raise _erro(
            "layout_sem_abas",
            "The grouped layout needs 'abas': the list of {aba, layout}, in file order.",
        )
    nomes: set[str] = {ABA_CARIMBO.casefold()}
    layouts: set[str] = set()
    for i, item in enumerate(abas, start=1):
        if not isinstance(item, dict):
            raise _erro("layout_aba_invalida", f"Sheet {i}: must be an object {{aba, layout}}.")
        for chave in item:
            if chave in _PROIBIDAS:
                _recusar_transformacao(chave, f"sheet {i}")
            if chave not in _CHAVES_ABA_AGRUPADA:
                raise _erro(
                    "layout_aba_invalida",
                    f"Sheet {i}: field '{chave}' is not allowed (aba, layout).",
                )
        nome = item.get("aba")
        if not isinstance(nome, str) or not nome.strip():
            raise _erro("layout_aba_invalida", f"Sheet {i}: provide 'aba' (the sheet name).")
        if _texto_e_formula(nome):
            _recusar_transformacao("formula in the sheet name", f"sheet {i}")
        if nome_aba(nome) != nome:
            # Silently sanitizing would give the sheet a name nobody wrote -- and consumers look
            # sheets up by name.
            raise _erro(
                "layout_aba_invalida",
                f"Sheet {i}: '{nome}' is not a valid Excel sheet name (up to 31 characters, no "
                "leading/trailing spaces and none of []:*?/\\).",
            )
        if nome.casefold() in nomes:
            raise _erro(
                "layout_aba_repetida",
                f"Sheet '{nome}' appears twice (or is '{ABA_CARIMBO}'); Excel does not "
                "distinguish case in sheet names.",
            )
        nomes.add(nome.casefold())
        try:
            lid = str(uuid.UUID(str(item.get("layout"))))
        except ValueError:
            raise _erro(
                "layout_aba_invalida", f"Sheet '{nome}': 'layout' must be a layout id (uuid)."
            ) from None
        if lid in layouts:
            raise _erro(
                "layout_aba_repetida",
                f"Layout {lid} appears in more than one sheet; each sheet organizes one source.",
            )
        layouts.add(lid)
    for flag in ("carimbo", "padrao"):
        if flag in definicao and not isinstance(definicao[flag], bool):
            raise _erro("layout_invalido", f"'{flag}' must be true or false.")
    for campo in ("nome", "descricao"):
        if campo in definicao and definicao[campo] is not None:
            if not isinstance(definicao[campo], str):
                raise _erro("layout_invalido", f"'{campo}' must be text.")
            if _texto_e_formula(definicao[campo]):
                _recusar_transformacao("formula", f"'{campo}'")


def _validar_bloco_fixo(definicao: dict) -> None:
    """Validates the NAMED exception: title, fixed rows and totals.

    The layer does not calculate -- and still does not. What `totais` allows is ONE sum of the
    report's own column, written as `=SUM(<col><first>:<col><last>)`. It accepts no expression:
    there is no field to write `A1*B2` in, and no function besides `soma`. It is the minimum that
    reproduces a sheet with a live total ABOVE the data, which no generic "total row" could build.

    `total`, `linha_total` and `subtotal` remain REFUSED: they are the names under which subtotal
    logic would come back into an ordinary layout.
    """
    titulo = definicao.get("titulo")
    if titulo is not None:
        if not isinstance(titulo, dict) or not isinstance(titulo.get("texto"), str):
            raise _erro("layout_invalido", "'titulo' requires 'texto'.")
        if _texto_e_formula(titulo["texto"]):
            _recusar_transformacao("formula", "'titulo.texto'")
        if set(titulo) - {"texto", "ate_coluna", "negrito"}:
            raise _erro("layout_invalido", "'titulo' accepts 'texto', 'ate_coluna' and 'negrito'.")

    for i, linha in enumerate(definicao.get("linhas_fixas") or []):
        if not isinstance(linha, dict) or not isinstance(linha.get("linha"), int):
            raise _erro("layout_invalido", f"'linhas_fixas[{i}]' requires 'linha' (integer).")
        if set(linha) - {
            "linha",
            "coluna",
            "texto",
            "valor",
            "formato",
            "negrito",
            "centralizado",
            "escala",
        }:
            raise _erro("layout_invalido", f"'linhas_fixas[{i}]' has an unknown key.")
        if linha.get("formato") and linha["formato"] not in FORMATOS:
            raise _erro("layout_formato_invalido", f"'linhas_fixas[{i}]': unknown format.")
        for campo in ("texto", "valor"):
            if isinstance(linha.get(campo), str) and _texto_e_formula(linha[campo]):
                _recusar_transformacao("formula", f"'linhas_fixas[{i}].{campo}'")
        escala = linha.get("escala")
        if escala is not None and (
            isinstance(escala, bool) or not isinstance(escala, int | float) or escala == 0
        ):
            # `escala` exists to put a value in points into a cell formatted as a percentage --
            # presentation only. Closed to numbers: a text field here would become the formula the
            # layer refuses.
            raise _erro("layout_invalido", f"'linhas_fixas[{i}].escala' is a number other than 0.")

    totais = definicao.get("totais")
    if totais is None:
        return
    if not isinstance(totais, dict):
        raise _erro("layout_invalido", "'totais' must be an object.")
    if set(totais) - {"linha", "colunas", "funcao", "negrito"}:
        raise _erro(
            "layout_invalido", "'totais' accepts 'linha', 'colunas', 'funcao' and 'negrito'."
        )
    if totais.get("funcao", "soma") != "soma":
        # Closed on purpose: with a free field here the layout would calculate again.
        raise _erro("layout_invalido", "'totais.funcao' only accepts 'soma'.")
    if not isinstance(totais.get("linha"), int):
        raise _erro("layout_invalido", "'totais' requires 'linha' (integer).")
    nomes = {c["coluna"] for c in definicao.get("colunas", [])}
    fora = [c for c in (totais.get("colunas") or []) if c not in nomes]
    if not totais.get("colunas") or fora:
        raise _erro(
            "layout_invalido",
            f"'totais.colunas' must name columns of the layout itself; outside it: {fora}.",
        )


#: `{{coluna.x}}` in the title or a fixed row: the (constant) value of a column of the snapshot
#: ITSELF. It lets a sheet show a figure the pipeline COMPUTED without it becoming a parameter of
#: whoever triggers the run. Reading a ready value transforms nothing -- the calculation lives in
#: the node; here the number is only written into a cell.
_RE_COLUNA_NO_PREAMBULO = re.compile(r"\{\{coluna\.([A-Za-z_][A-Za-z0-9_]*)(?::[^}]+)?\}\}")


def colunas_do_preambulo(definicao: dict) -> list[str]:
    """The columns the title and fixed rows cite via `{{coluna.x}}`, in order, without repeats."""
    textos = [(definicao.get("titulo") or {}).get("texto")]
    for f in definicao.get("linhas_fixas") or []:
        textos += [f.get("texto"), f.get("valor")]
    nomes: list[str] = []
    for texto in textos:
        if isinstance(texto, str):
            for nome in _RE_COLUNA_NO_PREAMBULO.findall(texto):
                if nome not in nomes:
                    nomes.append(nome)
    return nomes


def validar_contra_snapshot(definicao: dict, colunas_snapshot: list[str]) -> None:
    """Every layout column must exist in the snapshot -- an error, never an empty column. A column
    that "disappeared" between pipeline versions would come out blank and the report would look
    delivered. Also applies to columns the preamble cites: without it the cell would be empty,
    which is the same defect."""
    existentes = set(colunas_snapshot)
    faltando = [c["coluna"] for c in definicao.get("colunas", []) if c["coluna"] not in existentes]
    faltando += [
        c for c in colunas_do_preambulo(definicao) if c not in existentes and c not in faltando
    ]
    if faltando:
        raise _erro(
            "layout_coluna_inexistente",
            "Layout column does not exist in the node snapshot: "
            + ", ".join(f"'{c}'" for c in faltando)
            + ". Available: "
            + ", ".join(sorted(existentes))
            + ".",
        )


def colunas_do_layout(definicao: dict) -> list[str]:
    return [c["coluna"] for c in definicao.get("colunas", [])]


def rotulos_do_layout(definicao: dict) -> list[str]:
    """A declared EMPTY label is honored -- it does not become the column name. A column may
    deliberately have a blank header; falling back with `or` would write the column name there."""
    return [
        (c["rotulo"] if "rotulo" in c and c["rotulo"] is not None else c["coluna"])
        for c in definicao.get("colunas", [])
    ]


def rotulos2_do_layout(definicao: dict) -> list[str]:
    """Second header row. Empty where the column declares none -- the field is per column and
    nothing forces symmetry."""
    return [c.get("rotulo2") or "" for c in definicao.get("colunas", [])]


def formatos_do_layout(definicao: dict) -> list[str | None]:
    """`number_format` per column (None = Excel's General format)."""
    return [FORMATOS.get(c.get("formato") or "") for c in definicao.get("colunas", [])]
