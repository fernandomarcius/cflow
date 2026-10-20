"""Data rows of the xlsx without one object per cell -- the SAME XML openpyxl would write.

Measured: ~4.3 us per cell in `gerar_xlsx`. The cost is not serializing XML (with `lxml` it drops
only 9 %), it is openpyxl creating a `Cell`, a `StyleArray` and an `Element` per cell. A
million-row, 64-column report took several minutes.

**How it stays identical, byte for byte.** openpyxl still builds everything that is NOT a data
row -- header, preamble, styles, panes, filter, sheets, stamp. Data rows are written here, one
file per sheet, and enter the zip before `</sheetData>` after `save` (`injetar`). Nothing in the
skeleton depends on them: the write-only sheet has no `<dimension>`, and the `autoFilter` comes
from the row count, which `gerar` already knows. Each cell decision calls the SAME openpyxl parts:

- type via `_TYPES`/`get_type`; text via `check_string` (32,767-character cap and
  `IllegalCharacterError` on control characters); formula when the text starts with `=`;
  `ERROR_CODES` becomes `t="e"`;
- number via `safe_string` (`"%.16g"`), date via `to_excel`, text via `ET._escape_cdata` (what
  `et_xmlfile` uses) with `xml:space` per the `whitespace` rule;
- the style id comes from a template cell built with the same calls as `gerar`, holding its OWN
  value. And the registration ORDER is openpyxl's, in two phases per row: first the formatted
  cells are CREATED (registering number format -- including the automatic date format, which
  openpyxl sets and then overwrites -- and font, in column order); then the row is WRITTEN (the raw
  value is bound, registering its automatic date format, and each style id is assigned, also in
  column order). So style, format and font come out with the same indexes in `styles.xml`.

`core/tests/test_relatorio_xlsx_linhas.py` generates the same sheets through both paths and
compares every zip member, byte by byte.
"""

from __future__ import annotations

import math
import os
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from xml.etree.ElementTree import _escape_cdata

from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import _TYPES, ERROR_CODES, ILLEGAL_CHARACTERS_RE, TIME_TYPES, get_type
from openpyxl.compat import safe_string
from openpyxl.styles.numbers import is_date_format
from openpyxl.utils.datetime import to_excel
from openpyxl.utils.exceptions import IllegalCharacterError

_TETO_TEXTO = 32767  # `Cell.check_string`

#: openpyxl's `ERROR_CODES` is a TUPLE of 7, and the `v in ERROR_CODES` test runs for every text
#: cell -- up to seven string comparisons each. As a `frozenset` it is a hash lookup. Same content,
#: same answer.
_ERROS = frozenset(ERROR_CODES)


@lru_cache(maxsize=4096)
def _serial_data(v, epoch):
    """Memoized `to_excel`. It is PURE (date -> Excel serial number) and a date column repeats many
    values: thousands of rows fall on the same day. The key carries the `epoch` because it decides
    the result (1900 vs 1904)."""
    return to_excel(v, epoch)


# --- number text as the ClosedXML library rewrites it -----------------------------------------
#
# Some consumers merge several workbooks by opening each one (written here with `"%.16g"`) with the
# .NET ClosedXML library and rewriting it. ClosedXML stores a cell as a `double` and writes it with
# `double.ToString("G15", InvariantCulture)`. A DATE cell goes back first through `DateTime`
# (`DateTime.FromOADate`, which rounds to the millisecond) and is written via `ToOADate`. The three
# functions below redo that chain in Python, on the SAME double.

_MS_POR_DIA = 86_400_000
_TICKS_POR_MS = 10_000
_TICKS_POR_DIA = _MS_POR_DIA * _TICKS_POR_MS
_DESLOCAMENTO_OA_TICKS = 599_264_352_000_000_000  # `DateTime.DoubleDateOffset` (1899-12-30)
_DESLOCAMENTO_OA_MS = _DESLOCAMENTO_OA_TICKS // _TICKS_POR_MS


def g15(x: float) -> str:
    """.NET (Core 3.0+) `double.ToString("G15", CultureInfo.InvariantCulture)`: 15 significant
    digits rounded from the EXACT double value (ties to even), no trailing zeros, scientific
    notation when the exponent is < -4 or >= 15, with `E` and at least two exponent digits;
    negative zero is `-0`. It is Python's `"%.15g"` with an upper-case `e`."""
    if x == 0:
        return "-0" if math.copysign(1.0, x) < 0 else "0"
    texto = "%.15g" % x  # noqa: UP031 -- `%g` is the contract, not display formatting
    i = texto.find("e")
    return texto if i < 0 else f"{texto[:i]}E{texto[i + 1 :]}"


@lru_cache(maxsize=8192)
def _data_ida_e_volta(x: float) -> float:
    """The date serial after `DateTime.FromOADate` + `ToOADate` (with ClosedXML's 1900 adjustment).
    Only the sub-millisecond fraction changes; a whole day comes back unchanged. Outside the
    `DateTime` range ClosedXML would not even open the file: the value is returned as is."""
    if x >= 61.0:
        d = x
    elif x <= 60.0:
        d = x + 1.0
    else:
        return x
    if not (-657_435.0 < d < 2_958_466.0):
        return x
    # `DateTime.DoubleDateToTicks`: the arithmetic in double and the `(long)` truncation BEFORE the
    # (integer) offset; C#'s `%` takes the sign of the dividend.
    ms = int(d * _MS_POR_DIA + (0.5 if d >= 0 else -0.5))
    if ms < 0:
        ms -= int(math.fmod(ms, _MS_POR_DIA)) * 2
    ticks = (ms + _DESLOCAMENTO_OA_MS) * _TICKS_POR_MS
    # `DateTime.TicksToOADate`.
    if ticks == 0:
        y = 0.0
    else:
        if ticks < _TICKS_POR_DIA:
            ticks += _DESLOCAMENTO_OA_TICKS
        m = (
            ticks - _DESLOCAMENTO_OA_TICKS
        ) // _TICKS_POR_MS  # exact: ticks is a multiple of the ms
        if m < 0:
            frac = int(math.fmod(m, _MS_POR_DIA))
            if frac != 0:
                m -= (_MS_POR_DIA + frac) * 2
        y = m / _MS_POR_DIA
    return y if y > 60.0 else y - 1.0


def numero_closedxml(v, data: bool = False) -> str:
    """The text the rewritten file holds for a number openpyxl would write: openpyxl's `"%.16g"`
    re-read as a double, the date via the `DateTime` round trip, and `G15`."""
    x = float("%.16g" % v)  # noqa: UP031 -- openpyxl's `safe_string`
    return g15(_data_ida_e_volta(x) if data else x)


def compativel() -> bool:
    """This writer copies openpyxl 3.1's `etree_write_cell` (without `lxml`) -- the version pinned
    in the lock. With `lxml` in the environment openpyxl switches serializer, and in a newer
    version the private parts used here may change: in both cases `gerar` falls back to openpyxl
    cell by cell, which is the reference. Slower, never different."""
    import openpyxl

    return not openpyxl.LXML and openpyxl.__version__.startswith("3.1.")


_PRESERVA = ' xml:space="preserve"'


def letra_coluna(n: int) -> str:
    """1 → A, 27 → AA."""
    s = ""
    while n > 0:
        n, resto = divmod(n - 1, 26)
        s = chr(65 + resto) + s
    return s or "A"


def _tipo(v) -> str:
    """`Cell._bind_value`: the cell type from the value type (with openpyxl's own cache)."""
    if v is None:
        return "n"
    t = type(v)
    try:
        dt = _TYPES[t]
    except KeyError:
        dt = get_type(t, v)
    if dt is None:
        raise ValueError(f"Cannot convert {v!r} to Excel")
    if dt == "s":
        v = _texto(v)
        if len(v) > 1 and v.startswith("="):
            return "f"
        if v in _ERROS:
            return "e"
    return dt


def _texto(v: str) -> str:
    """`Cell.check_string` for a `str`."""
    v = v[:_TETO_TEXTO]
    if ILLEGAL_CHARACTERS_RE.search(v):
        raise IllegalCharacterError(f"{v} cannot be used in worksheets.")
    return v


def xml_celula(ref: str, v, dt: str, sid: int | None, epoch) -> str:
    """A `<c>` as `etree_write_cell` would write it. `dt` is the type after `_bind_value`
    (`s`, `f`, `e`, `n`, `b`, `d`); `sid` is the style id when the cell has a style."""
    s = "" if sid is None else f' s="{sid}"'
    if dt == "d":
        v = _serial_data(v, epoch)
        t = ' t="n"'
    elif dt == "s":
        t = ' t="inlineStr"'
    elif dt == "f":
        t = ""
    else:
        t = f' t="{dt}"'
    if v is None or v == "":
        return f'<c r="{ref}"{s}{t} />'
    if dt == "f":
        return f'<c r="{ref}"{s}{t}><f>{_escape_cdata(v[1:])}</f><v /></c>'
    if dt == "s":
        limpo = v.strip()
        espaco = _PRESERVA if limpo and v != limpo else ""
        return f'<c r="{ref}"{s}{t}><is><t{espaco}>{_escape_cdata(v)}</t></is></c>'
    texto = safe_string(v)
    if not texto:
        return f'<c r="{ref}"{s}{t}><v /></c>'
    return f'<c r="{ref}"{s}{t}><v>{_escape_cdata(texto)}</v></c>'


def _cru(ws, v) -> WriteOnlyCell:
    """Template cell for the raw value: openpyxl's `_values_to_row` assigns the value to a clean
    cell, and it is `_bind_value` that sets the automatic date format."""
    c = WriteOnlyCell(ws)
    c.value = v
    return c


def _numerica(ws, v, fmt: str) -> WriteOnlyCell:
    """Template cell for `gerar_xlsx`'s `WriteOnlyCell(ws, value=v)` + `number_format` path."""
    c = WriteOnlyCell(ws, value=v)
    c.number_format = fmt
    return c


class Estilos:
    """Template cells and style ids per combination, for the whole WORKBOOK (all sheets).

    `criar` is the creation phase (registers number format and font); `id` is the write phase
    (registers the cell style). Separate because openpyxl does them at different moments of the
    row."""

    def __init__(self) -> None:
        self._modelos: dict[tuple, WriteOnlyCell] = {}
        self._ids: dict[tuple, int] = {}

    def criar(self, chave: tuple, fabrica: Callable[[], WriteOnlyCell]) -> None:
        if chave not in self._modelos:
            self._modelos[chave] = fabrica()

    def id(self, chave: tuple) -> int:
        sid = self._ids.get(chave)
        if sid is None:
            sid = self._ids[chave] = self._modelos[chave].style_id
        return sid


class LinhasDaAba:
    """The data rows of ONE sheet, as XML, in a temporary file.

    `colunas`: `(format, bold)` per layout column, in order; `tamanho` is the layout's font size
    (`fonte_tamanho`). The three cell paths are those of `gerar_xlsx`: with no format, bold or size
    the value goes raw (and only a date gets a style, the automatic one); with bold or size,
    `_celula_formatada`; with format only, `WriteOnlyCell` + `number_format`."""

    def __init__(
        self,
        ws,
        colunas: list[tuple[str | None, bool]],
        *,
        tamanho,
        vazias: int,
        primeira_linha: int,
        estilos: Estilos,
        celula: Callable,
        celula_formatada: Callable,
        closedxml: bool = False,
    ) -> None:
        self._ws = ws
        # ClosedXML shape (see `gerar.gerar_xlsx_agrupado`): empty text becomes a blank cell, the
        # trailing empty column has no cell and numbers go through `numero_closedxml`.
        self._closedxml = closedxml
        self._epoch = ws.parent.epoch
        self._tamanho = tamanho
        self._estilos = estilos
        self._celula = celula
        self._formatada = celula_formatada
        # (letter, format, bold, formatted?, {category: style id}) -- "formatted" = cell created
        # BEFORE the write; the dict holds the id already resolved per value category.
        self._colunas = [
            (
                letra_coluna(j),
                fmt,
                bool(negrito),
                bool(fmt is not None or negrito or tamanho),
                {},
            )
            for j, (fmt, negrito) in enumerate(colunas, 1)
        ]
        # Date-format column: in ClosedXML its number is a `DateTime` (`numero_closedxml`).
        self._datas = [bool(fmt) and is_date_format(fmt) for fmt, _ in colunas]
        self._vazias = (
            [] if closedxml else [letra_coluna(len(colunas) + k) for k in range(1, vazias + 1)]
        )
        self._linha = primeira_linha
        fd, caminho = tempfile.mkstemp(prefix="cflow_linhas_", suffix=".xml")
        self.caminho = Path(caminho)
        self._arquivo = os.fdopen(fd, "w", encoding="utf-8", newline="")

    def _fabrica(self, fmt, negrito, v) -> Callable[[], WriteOnlyCell]:
        ws, tamanho = self._ws, self._tamanho
        if negrito or tamanho:
            return lambda: self._formatada(ws, v, formato=fmt, negrito=negrito, tamanho=tamanho)
        return lambda: _numerica(ws, v, fmt)

    def _valor(self, bruto):
        """`gerar._celula` with the common types resolved here (the rest goes to that function)."""
        t = type(bruto)
        if bruto is None or t is str or t is int or t is bool or t is Decimal:
            return bruto
        if t is float:
            return bruto if math.isfinite(bruto) else None
        return self._celula(bruto)

    def _linha_xml(self, rs: str, brutos) -> str:
        estilos = self._estilos
        valores = [self._valor(b) for b in brutos]
        if self._closedxml:
            valores = [None if v == "" and type(v) is str else v for v in valores]
        # CREATION phase: formatted cells are born before the row is written. It only matters the
        # FIRST time for each combination (afterwards registration is idempotent): `sids` keeps,
        # per column, the id already resolved for each value category.
        categorias: list = []
        for (_, fmt, negrito, formatada, sids), v in zip(self._colunas, valores, strict=True):
            if not formatada:
                categorias.append(None)
                continue
            cat = type(v) if isinstance(v, TIME_TYPES) else None
            categorias.append(cat)
            if cat not in sids:
                estilos.criar((fmt, negrito, cat), self._fabrica(fmt, negrito, v))
        # WRITE phase, in column order.
        celulas = []
        ap = celulas.append
        if self._closedxml:
            return self._linha_closedxml(rs, valores, categorias)
        for (letra, fmt, negrito, formatada, sids), v, cat in zip(
            self._colunas, valores, categorias, strict=True
        ):
            if formatada:
                sid = sids.get(cat)
                if sid is None:
                    sid = sids[cat] = estilos.id((fmt, negrito, cat))
                if sid == 0:
                    # Default style = a cell WITHOUT style for openpyxl (`has_style` false): the `s`
                    # disappears and, with a null value, the cell is not written at all.
                    # Unreachable with the current vocabulary (no format is "general"); a guard
                    # for when one is.
                    if v is not None:
                        ap(self._xml(letra + rs, v, None))
                    continue
                ap(self._xml(letra + rs, v, sid))
            elif v is not None:
                ap(self._xml(letra + rs, v, None))
        celulas.extend(f'<c r="{letra}{rs}" t="inlineStr" />' for letra in self._vazias)
        return f'<row r="{rs}">{"".join(celulas)}</row>'

    def _linha_closedxml(self, rs: str, valores: list, categorias: list) -> str:
        """The `_linha_xml` row, with numbers in ClosedXML text form and without the empty column."""
        celulas = []
        ap = celulas.append
        for (letra, fmt, negrito, formatada, sids), v, cat, data in zip(
            self._colunas, valores, categorias, self._datas, strict=True
        ):
            sid = None
            if formatada:
                sid = sids.get(cat)
                if sid is None:
                    sid = sids[cat] = self._estilos.id((fmt, negrito, cat))
                if sid == 0:
                    sid = None
            if v is None:
                if sid is not None:
                    ap(f'<c r="{letra}{rs}" s="{sid}" t="n" />')
                continue
            t = type(v)
            if t is int or t is float or t is Decimal:
                s = "" if sid is None else f' s="{sid}"'
                ap(f'<c r="{letra}{rs}"{s} t="n"><v>{numero_closedxml(v, data)}</v></c>')
            elif t is not str and t is not bool and isinstance(v, TIME_TYPES):
                ap(self._xml_data(letra + rs, v, sid))
            else:
                ap(self._xml(letra + rs, v, sid))
        return f'<row r="{rs}">{"".join(celulas)}</row>'

    def _xml_data(self, ref: str, v, sid: int | None) -> str:
        """Parquet date/time: the serial openpyxl would write, through the ClosedXML round trip.
        With no column format the style is openpyxl's automatic date one (as in `_xml`)."""
        if sid is None:
            chave = ("cru", type(v))
            self._estilos.criar(chave, lambda: _cru(self._ws, v))
            sid = self._estilos.id(chave)
        dt = _tipo(v)
        if (
            dt != "d"
        ):  # openpyxl writes `time`/`timedelta` as a number without going through DateTime
            return xml_celula(ref, v, dt, sid, self._epoch)
        serial = _serial_data(v, self._epoch)
        return f'<c r="{ref}" s="{sid}" t="n"><v>{numero_closedxml(serial, True)}</v></c>'

    def _xml(self, ref: str, v, sid: int | None) -> str:
        """`xml_celula` dispatching on the EXACT value type; anything uncommon goes to it. The texts
        are the same: number via `"%.16g"` (openpyxl's `safe_string`), text via `check_string` +
        `_escape_cdata`. A raw date value gets the automatic style here."""
        t = type(v)
        s = "" if sid is None else f' s="{sid}"'
        if t is str:
            v = _texto(v)
            if len(v) > 1 and v[0] == "=":
                return f'<c r="{ref}"{s}><f>{_escape_cdata(v[1:])}</f><v /></c>'
            if v in _ERROS:
                return f'<c r="{ref}"{s} t="e"><v>{_escape_cdata(v)}</v></c>'
            if not v:
                return f'<c r="{ref}"{s} t="inlineStr" />'
            limpo = v.strip()
            espaco = _PRESERVA if limpo and v != limpo else ""
            return f'<c r="{ref}"{s} t="inlineStr"><is><t{espaco}>{_escape_cdata(v)}</t></is></c>'
        if t is int or t is float:
            # The SAME `"%.16g" % v` as openpyxl's `safe_string`, on purpose: it is the contract.
            return f'<c r="{ref}"{s} t="n"><v>{"%.16g" % v}</v></c>'  # noqa: UP031
        if t is bool:
            return f'<c r="{ref}"{s} t="b"><v>{"1" if v else "0"}</v></c>'
        if v is None:
            return f'<c r="{ref}"{s} t="n" />'
        dt = _tipo(v)
        if dt == "d" and sid is None:
            chave = ("cru", t)
            self._estilos.criar(chave, lambda: _cru(self._ws, v))
            sid = self._estilos.id(chave)
        return xml_celula(ref, v, dt, sid, self._epoch)

    def escrever(self, linhas) -> int:
        """`linhas`: iterable of Parquet value tuples, in layout column order."""
        partes: list[str] = []
        for valores in linhas:
            partes.append(self._linha_xml(str(self._linha), valores))
            self._linha += 1
        self._arquivo.write("".join(partes))
        return len(partes)

    def fechar(self) -> Path:
        if not self._arquivo.closed:
            self._arquivo.close()
        return self.caminho


def injetar(
    destino: Path,
    blocos: dict[str, Path],
    *,
    dimensoes: dict[str, str] | None = None,
    impressao: set[str] | None = None,
) -> None:
    """Rewrites the xlsx with each sheet's rows before its `</sheetData>`. The zip is rebuilt member
    by member (same order, name, date and compression); the rows are streamed, without loading
    their file into memory.

    `dimensoes` (`{member: "A1:BE631433"}`): the `<dimension>` the sheet declares, before
    `<sheetViews>` (schema order). openpyxl in `write_only` does not write it.

    `impressao` (members): the sheets that get ClosedXML's print configuration
    (`_com_impressao_closedxml`)."""
    dimensoes = dimensoes or {}
    impressao = impressao or set()
    tmp = destino.with_name(f".{destino.name}.linhas.tmp")
    try:
        with zipfile.ZipFile(destino) as zin, zipfile.ZipFile(tmp, "w", allowZip64=True) as zout:
            for info in zin.infolist():
                dados = zin.read(info)
                bloco = blocos.get(info.filename)
                dimensao = dimensoes.get(info.filename)
                if dimensao is not None:
                    dados = _com_dimensao(info.filename, dados, dimensao)
                if info.filename in impressao:
                    dados = _com_impressao_closedxml(info.filename, dados)
                if bloco is None:
                    zout.writestr(info, dados)
                    continue
                corte = dados.rfind(b"</sheetData>")
                if corte < 0:
                    raise ValueError(f"{info.filename}: sheet without </sheetData> for the rows")
                novo = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                novo.compress_type = info.compress_type
                novo.external_attr = info.external_attr
                with zout.open(novo, "w", force_zip64=True) as saida:
                    saida.write(dados[:corte])
                    with open(bloco, "rb") as linhas:
                        shutil.copyfileobj(linhas, saida, 1 << 20)
                    saida.write(dados[corte:])
        os.replace(tmp, destino)
    finally:
        tmp.unlink(missing_ok=True)


def _com_dimensao(nome: str, dados: bytes, ref: str) -> bytes:
    marca = dados.find(b"<sheetViews")
    if marca < 0:
        marca = dados.find(b"<sheetFormatPr")
    if marca < 0 or b"<dimension" in dados[:marca]:
        raise ValueError(f"{nome}: nowhere to declare the sheet's <dimension>")
    return dados[:marca] + f'<dimension ref="{ref}" />'.encode() + dados[marca:]


# Print parts as ClosedXML 0.105 writes them when rewriting a sheet: Letter paper
# (`paperSize="1"`), scale 100, and explicit `printOptions`/`headerFooter` with default values.
# Without them the grouped file would print on the printer's default paper. `<tableParts
# count="0">` is left out: it is empty and has no effect.
_PRINT_OPTIONS_CLOSEDXML = (
    b'<printOptions horizontalCentered="0" verticalCentered="0" headings="0" gridLines="0" />'
)
_PAGE_SETUP_CLOSEDXML = (
    b'<pageSetup paperSize="1" scale="100" pageOrder="downThenOver" orientation="default" '
    b'blackAndWhite="0" draft="0" cellComments="none" errors="displayed" />'
)
_HEADER_FOOTER_CLOSEDXML = (
    b'<headerFooter differentOddEven="0" differentFirst="0" scaleWithDoc="1" alignWithMargins="1">'
    b"<oddHeader /><oddFooter /><evenHeader /><evenFooter /><firstHeader /><firstFooter />"
    b"</headerFooter>"
)


def _com_impressao_closedxml(nome: str, dados: bytes) -> bytes:
    """`printOptions` before and `pageSetup`/`headerFooter` after `<pageMargins>` (schema order),
    and without the empty `<pageSetUpPr />` that openpyxl puts in `<sheetPr>` and ClosedXML does
    not."""
    for tag in (b"<printOptions", b"<pageSetup ", b"<headerFooter"):
        if tag in dados:
            raise ValueError(f"{nome}: the sheet already declares {tag.decode()}; not overwriting")
    ini = dados.find(b"<pageMargins")
    fim = dados.find(b"/>", ini) + 2 if ini >= 0 else -1
    if ini < 0 or fim < 2:
        raise ValueError(f"{nome}: sheet without <pageMargins> to anchor the print settings")
    dados = (
        dados[:ini]
        + _PRINT_OPTIONS_CLOSEDXML
        + dados[ini:fim]
        + _PAGE_SETUP_CLOSEDXML
        + _HEADER_FOOTER_CLOSEDXML
        + dados[fim:]
    )
    return dados.replace(b"<pageSetUpPr />", b"", 1)
