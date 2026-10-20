"""Conversion profiles per source FAMILY: how bronze text becomes numbers and dates.

Automatic inference decides **whether** a column is numeric or a date. It cannot decide **how** to
convert, because of fixed-width layouts: in a field like `PRICE = '0000000003227'` the value is
**32.27**, with an implied two-digit decimal. A direct `TRY_CAST` returns 3227 and **does not
fail**. This is not data loss; it is data a hundred times wrong that looks right, the worst possible
outcome for a financial consumer.

So the "how" is DECLARED here, reviewable, instead of guessed from content. A family is selected by
the dataset name prefix:

| family  | prefix    | date         | decimal         | scale |
|---------|-----------|--------------|-----------------|-------|
| iso     | (default) | `2026-01-01` | `1095646.22`    | 1     |
| dmy     | `dmy_`    | `01/12/2019` | `1.15`          | 1     |
| api     | `api_`    | `2026-01-01` | `5.0717`        | 1     |
| comma   | `comma_`  | `14/08/2026` | `0,693`         | 1     |
| fixed   | `fixed_`  | `20260814`   | `0000000003227` | 100   |

Replace or extend the families to match your own sources.
"""

from __future__ import annotations

from dataclasses import dataclass

# Texts a source uses to say "there is no value". Without declaring them, a whole column stays
# VARCHAR because of a handful of rows; with them, the sentinel becomes NULL (which is what it
# means) and the rest is typed.
AUSENTES_PADRAO: tuple[str, ...] = ("", "-", "--", "N/D", "N/A", "n/a", "NA", "nan")


@dataclass(frozen=True)
class Perfil:
    """How to convert a family's text. `escala` divides the integer read (implied decimal)."""

    nome: str
    decimal_virgula: bool = False
    formatos_data: tuple[str, ...] = ("%Y-%m-%d",)
    escala: int = 1
    # Columns inference must NEVER type as a number, even when 100 % convert: an identifier with
    # leading zeros becomes a different identifier once it is an integer.
    nunca_numero: tuple[str, ...] = ()
    ausentes: tuple[str, ...] = AUSENTES_PADRAO
    # Fixed-width layouts pad numbers with leading zeros (`0000000003227` = 32.27). Where that
    # holds, leading zeros and length do NOT indicate an identifier, they indicate padding, and the
    # format veto must stay off or it would veto every price.
    padding_zeros: bool = False


PERFIS: dict[str, Perfil] = {
    "iso": Perfil(
        nome="iso",
        formatos_data=("%Y-%m-%d",),
        nunca_numero=("TAX_ID", "CODE", "ID_", "_ID", "ISIN", "CD_"),
    ),
    "dmy": Perfil(nome="dmy", formatos_data=("%d/%m/%Y",)),
    "api": Perfil(nome="api", formatos_data=("%Y-%m-%d",)),
    "comma": Perfil(
        nome="comma",
        decimal_virgula=True,
        formatos_data=("%d/%m/%Y",),
        nunca_numero=("Code", "TAX_ID", "INDEX"),
    ),
    # The reason this module exists. `escala=100`: price and volume arrive as an integer with two
    # implied decimals. The trade date is `YYYYMMDD` with no separator.
    "fixed": Perfil(
        nome="fixed",
        formatos_data=("%Y%m%d",),
        escala=100,
        nunca_numero=("TICKER", "ISINCODE", "MARKETCODE", "TYPE", "MONTHS", "RECORD", "TERM"),
        padding_zeros=True,
    ),
}

# Fixed-width columns that have NO implied decimal despite being numeric: counts and quantities are
# real integers. Dividing by 100 here would be wrong in the other direction.
FIXED_SEM_ESCALA = ("TOTTRADES", "TOTQTY", "FACTOR", "OPTIND")


def familia_de(dataset: str) -> str:
    """Family from the dataset name. It is the prefix the catalog already uses."""
    if dataset.startswith("dmy_"):
        return "dmy"
    if dataset.startswith("api_"):
        return "api"
    if dataset.startswith("comma_"):
        return "comma"
    if dataset.startswith("fixed_"):
        return "fixed"
    return "iso"  # conservative default: direct cast, no scale


def perfil_de(dataset: str) -> Perfil:
    return PERFIS[familia_de(dataset)]


def _quotar(coluna: str) -> str:
    return '"' + coluna.replace('"', '""') + '"'


def expressao_presente(coluna: str, perfil: Perfil) -> str:
    """The column's real value: the absence sentinel becomes `NULL`.

    It is also the DENOMINATOR of the typing criterion: without it, `--` counts as a value that
    fails to convert and the whole column stays text because of rows where the source said "none"."""
    col = _quotar(coluna)
    if not perfil.ausentes:
        return col
    lista = ", ".join("'" + s.replace("'", "''") + "'" for s in perfil.ausentes)
    return f"CASE WHEN trim({col}) IN ({lista}) THEN NULL ELSE trim({col}) END"


def expressao_numero(coluna: str, perfil: Perfil, *, dataset: str = "") -> str:
    """SQL that converts the column to `DECIMAL(28,10)` according to the profile.

    The scale is applied by DIVISION after the cast, not by `CAST(... AS DECIMAL(x,2))`: the text
    `0000000003227` has no dot, so the cast reads it as the integer 3227; the decimal point has to
    be repositioned arithmetically."""
    presente = expressao_presente(coluna, perfil)
    bruto = f"replace({presente}, ',', '.')" if perfil.decimal_virgula else presente
    expr = f"TRY_CAST({bruto} AS DECIMAL(28,10))"
    escala = perfil.escala
    if escala != 1 and coluna.upper() in FIXED_SEM_ESCALA:
        escala = 1
    if escala != 1:
        expr = f"({expr} / {escala})"
    return expr


def expressao_data(coluna: str, perfil: Perfil) -> str:
    """SQL that converts the column to `DATE`. Tries the profile's formats, in order."""
    col = expressao_presente(coluna, perfil)
    if perfil.formatos_data == ("%Y-%m-%d",):
        return f"TRY_CAST({col} AS DATE)"
    tentativas = [f"TRY_CAST(try_strptime({col}, '{f}') AS DATE)" for f in perfil.formatos_data]
    return tentativas[0] if len(tentativas) == 1 else f"COALESCE({', '.join(tentativas)})"


def pode_ser_numero(coluna: str, perfil: Perfil) -> bool:
    """An identifier does not become a number even if it converts: a tax ID with leading zeros stops
    being the same ID, and a numeric trading code stops matching the registry."""
    alvo = coluna.upper()
    return not any(marca.upper() in alvo for marca in perfil.nunca_numero)
