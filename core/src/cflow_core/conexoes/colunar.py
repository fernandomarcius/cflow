"""COLUMNAR extraction of a connection source: from the driver cursor to a text Arrow table.

The classic path (`amostra` -> per-value `serial` -> list of dicts -> `engine._carregar`) made four
Python passes over every value and kept up to three copies of the whole extraction at once: the
driver's list of dicts, the `dict(r)` copy, the `serial` dict and the per-column lists. And the
`serial` conversion ran ON THE EVENT LOOP, outside `to_thread`. Measured on a replay of a large
table (hundreds of thousands of rows): tens of seconds and several GB of peak memory with no
network at all, and every node running alongside stalled because the loop was busy converting.

Here the SAME conversion is fused into one pass per column, batch by batch (`fetchmany`), and each
batch becomes Arrow before the next one is read: the extraction never exists whole as Python objects.

**The same text, byte for byte.** `texto(v)` is `serial(v)` followed by `engine._texto_como_json`:
`Decimal` becomes `repr(float(d))`, dates become ISO, `bool` becomes `true`/`false`, `float` becomes
`repr`, `bytes` becomes hex. Whatever the classic path would send to JSON (NaN, infinity, a type it
does not know, a column name that breaks the spec) raises `ForaDoColunar` here, and the caller falls
back to the classic path, so the result is the same by construction in both cases
(`core/tests/test_extracao_colunar.py` compares one against the other).
"""

from __future__ import annotations

import math
import struct
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from operator import itemgetter

#: Rows per `fetchmany` batch. The batch is the only part of the extraction that exists as Python
#: objects; 50k rows x 40 columns fit in ~200 MB and amortise the fixed per-batch cost.
LOTE_LINHAS = 50_000


class ForaDoColunar(Exception):
    """The data has something only the classic (JSON) path knows how to handle."""


@dataclass
class Extracao:
    """Result of reading a source IN FULL. `tabela` (Arrow, every column text) when the connector can
    extract columnar; otherwise `linhas` (list of dicts, the `amostra` contract)."""

    colunas: list[str]
    tabela: object | None = None
    linhas: list[dict] | None = None


def colunas_validas(cols: list) -> list[str]:
    """Dedupes and drops empty/None names (phantom spreadsheet columns); `read_json` with
    `columns` rejects duplicate names."""
    vistos: set = set()
    cols_ok = []
    for c in cols:
        cn = str(c)
        if cn in ("", "None") or cn in vistos:
            continue
        vistos.add(cn)
        cols_ok.append(cn)
    return cols_ok


def nomes_colunares(cols_ok: list[str]) -> bool:
    """Do the names allow a columnar load? Same refusals as `engine._carregar_colunar`: a name with
    quotes or backslashes breaks the `read_json` `spec` (there it falls back to `read_json_auto`), and
    two names differing only in case are treated by DuckDB as the same."""
    if not cols_ok:
        return False
    if any(ch in c for c in cols_ok for ch in "\"'\\"):
        return False
    return len({c.casefold() for c in cols_ok}) == len(cols_ok)


def texto(v):
    """A driver value as the classic path leaves it in VARCHAR (`serial` + JSON)."""
    t = type(v)
    if t is str or v is None:
        return v
    if t is bool:
        return "true" if v else "false"
    if t is int:
        return str(v)
    if t is float:
        if math.isfinite(v):
            return repr(v)
        raise ForaDoColunar
    # From here down, the order and the `isinstance` checks are those of `base.serial`.
    if isinstance(v, datetime | date | time):
        return v.isoformat()
    if isinstance(v, Decimal):
        f = float(v)
        if math.isfinite(f):
            return repr(f)
        raise ForaDoColunar
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, bytes | bytearray | memoryview):
        return bytes(v).hex()
    # `serial` would return the value as is, and JSON would only serialise it via `default=str`:
    # a type the columnar path cannot guarantee to reproduce.
    raise ForaDoColunar


def _float_finito_texto(f: float) -> str:
    if math.isfinite(f):
        return repr(f)
    raise ForaDoColunar


def _decimal_texto(d: Decimal) -> str:
    return _float_finito_texto(float(d))


#: Converter for a SINGLE-type column: the text of `texto(v)` without per-value type dispatch.
#: Keyed by the EXACT type (`type(v) is t`): `bool` subclasses `int` and `datetime` subclasses
#: `date`, and each has its own text.
_CONVERSOR_POR_TIPO = {
    int: int.__str__,
    float: _float_finito_texto,
    Decimal: _decimal_texto,
    datetime: datetime.isoformat,
    date: date.isoformat,
    time: time.isoformat,
    uuid.UUID: uuid.UUID.__str__,
}


_I16 = struct.Struct("<h")
_U16 = struct.Struct("<H")


#: `pytds` version whose internals (reader `_buf`/`_pos`/`_size`, `res_info.columns`, the
#: serialisers) `acelerar_leitura` knows. Any other version: nothing is swapped. An upgrade that
#: changed the meaning of those attributes would read garbage WITHOUT raising `AttributeError`, so
#: an attribute guard would not be enough. The core `pyproject` pins the same version.
PYTDS_CONHECIDO = "1.17.1"


def _pytds_conhecido() -> bool:
    global _PYTDS_OK
    if _PYTDS_OK is None:
        try:
            from importlib.metadata import version

            _PYTDS_OK = version("python-tds") == PYTDS_CONHECIDO
        except Exception:
            _PYTDS_OK = False
    return _PYTDS_OK


_PYTDS_OK: bool | None = None


def acelerar_leitura(cur) -> int:
    """On an already-executed `pytds` cursor, swaps the reading of DECIMAL/NUMERIC, `varchar` and
    `nvarchar` (non-MAX) columns for one that reads straight from the packet buffer and returns the
    final columnar text. Returns how many columns it swapped.

    Measured in the profile of a real extraction: `pytds` decoding is 93 % of the time, and the cost
    is Python per value: every `get_byte`/`get_smallint` goes through `unpack -> readall_fast ->
    read_fast`, and every `readall` builds a generator and a `join`. Here, a value that fits wholly
    in the current packet comes from a slice of `_buf`; one that crosses the packet boundary (rare)
    goes through the ORIGINAL `read`, from the same position: the position only advances after the
    whole value has been read.

    **The same text, by construction.**
    - DECIMAL: `texto(d)` is `repr(float(d))`; `float(Decimal)` and `n / 10**scale` (integers) are
      the SAME double, the correct rounding of the same rational (the `pytds` `Decimal` is exact:
      precision 38 covers the column's 38 digits). Negative zero comes out as `-0.0` in both.
    - `varchar`/`nvarchar`: the same bytes through the SAME codec (the column `_codec` / UTF-16-LE).

    Only call it AFTER choosing the columnar path: DECIMAL becomes `str`, and the dict path
    (`serial`) expects `Decimal`. The serialiser belongs to the query (`from_stream` creates one per
    result column), so the swap does not leak into the next one. `pytds` internal API: without the
    expected attributes nothing is swapped and reading proceeds as usual."""
    if not _pytds_conhecido():
        return 0
    try:
        from pytds import tds_base
        from pytds.tds_types import (
            BitNSerializer,
            DateTimeNSerializer,
            IntNSerializer,
            MsDecimalSerializer,
            NVarChar70Serializer,
            NVarCharMaxSerializer,
            VarChar70Serializer,
            VarCharMaxSerializer,
        )

        sessao = cur._session
        colunas = sessao.res_info.columns
        unicode_ = bool(sessao._tds._login.bytes_to_unicode)
        sem_fuso = sessao.tzinfo_factory is None
    except (AttributeError, ImportError):
        return 0
    readall = tds_base.readall
    trocadas = 0
    for col in colunas:
        ser = getattr(col, "serializer", None)
        original = getattr(ser, "read", None)
        if original is None:
            continue
        tipo = type(ser)
        if tipo is MsDecimalSerializer and isinstance(getattr(ser, "scale", None), int):
            ser.read = _leitor_decimal(10**ser.scale, readall)
        elif isinstance(ser, NVarChar70Serializer) and not isinstance(ser, NVarCharMaxSerializer):
            ser.read = _leitor_nvarchar(original)
        elif (
            unicode_
            and isinstance(ser, VarChar70Serializer)
            and not isinstance(ser, VarCharMaxSerializer)
            and getattr(ser, "_codec", None) is not None
        ):
            ser.read = _leitor_varchar(ser._codec.decode, original)
        elif tipo is IntNSerializer:
            ser.read = _leitor_intn(original)
        elif tipo is BitNSerializer:
            ser.read = _leitor_bitn(original)
        elif tipo is DateTimeNSerializer and sem_fuso:
            ser.read = _leitor_datetimen(original)
        else:
            continue
        trocadas += 1
    return trocadas


#: Old name, kept for existing callers.
acelerar_decimais = acelerar_leitura


def _leitor_decimal(pot: int, readall):
    def _ler(r):
        buf, pos, fim = r._buf, r._pos, r._size
        if pos < fim:
            tamanho = buf[pos]
            if tamanho == 0:
                r._pos = pos + 1
                return None
            if fim - pos > tamanho:
                corpo = buf[pos + 2 : pos + 1 + tamanho]
                positivo = buf[pos + 1]
                r._pos = pos + 1 + tamanho
            else:
                corpo = None
        else:
            corpo = None
        if corpo is None:
            tamanho = r.get_byte()
            if tamanho <= 0:
                return None
            dados = readall(r, tamanho)
            positivo, corpo = dados[0], dados[1:]
        n = int.from_bytes(corpo, "little")
        if not positivo:
            if not n:
                return "-0.0"
            n = -n
        return repr(n / pot)

    return _ler


def _leitor_nvarchar(original):
    def _ler(r):
        buf, pos, fim = r._buf, r._pos, r._size
        if fim - pos >= 2:
            (n,) = _U16.unpack_from(buf, pos)
            if n == 0xFFFF:
                r._pos = pos + 2
                return None
            if fim - pos - 2 >= n:
                r._pos = pos + 2 + n
                return buf[pos + 2 : pos + 2 + n].decode("utf-16-le")
        return original(r)

    return _ler


#: Struct format by INTN size (tinyint is UNSIGNED in TDS).
_INTN = {
    1: struct.Struct("<B"),
    2: struct.Struct("<h"),
    4: struct.Struct("<l"),
    8: struct.Struct("<q"),
}
_DATETIME8 = struct.Struct("<ll")
_BASE_DATETIME = datetime(1900, 1, 1)


def _leitor_intn(original):
    """`str(int)`: the text of `texto(int)`."""

    def _ler(r):
        buf, pos, fim = r._buf, r._pos, r._size
        if pos < fim:
            tamanho = buf[pos]
            if tamanho == 0:
                r._pos = pos + 1
                return None
            formato = _INTN.get(tamanho)
            if formato is not None and fim - pos > tamanho:
                r._pos = pos + 1 + tamanho
                return str(formato.unpack_from(buf, pos + 1)[0])
        valor = original(r)
        return None if valor is None else str(valor)

    return _ler


def _leitor_bitn(original):
    """`'true'`/`'false'`: the text of `texto(bool)`; `pytds` does `bool(byte)`."""

    def _ler(r):
        buf, pos, fim = r._buf, r._pos, r._size
        if fim - pos >= 2 and buf[pos] == 1:
            r._pos = pos + 2
            return "true" if buf[pos + 1] else "false"
        if pos < fim and buf[pos] == 0:
            r._pos = pos + 1
            return None
        valor = original(r)
        return None if valor is None else ("true" if valor else "false")

    return _ler


def _leitor_datetimen(original):
    """`isoformat()` of the SAME `datetime` that `pytds` builds (`DateTime.to_pydatetime`, same
    arithmetic). Only the 8-byte DATETIME takes the fast path; SMALLDATETIME and packet boundaries go
    through the original. Installed only without `tzinfo_factory` (with a time zone the value changes,
    and so does the text)."""

    def _ler(r):
        buf, pos, fim = r._buf, r._pos, r._size
        if pos < fim:
            tamanho = buf[pos]
            if tamanho == 0:
                r._pos = pos + 1
                return None
            if tamanho == 8 and fim - pos > 8:
                dias, t = _DATETIME8.unpack_from(buf, pos + 1)
                r._pos = pos + 9
                ms = int(round(t % 300 * 10 / 3.0))
                return (
                    _BASE_DATETIME + timedelta(days=dias, seconds=t // 300, milliseconds=ms)
                ).isoformat()
        valor = original(r)
        return None if valor is None else valor.isoformat()

    return _ler


def _leitor_varchar(decodificar, original):
    def _ler(r):
        buf, pos, fim = r._buf, r._pos, r._size
        if fim - pos >= 2:
            (n,) = _I16.unpack_from(buf, pos)
            if n < 0:
                r._pos = pos + 2
                return None
            if fim - pos - 2 >= n:
                r._pos = pos + 2 + n
                return decodificar(buf[pos + 2 : pos + 2 + n])[0]
        return original(r)

    return _ler


def coluna_texto(valores):
    """A whole column. A text-only column (the most common case) passes through without touching any
    value: the type test is a `set(map(type, ...))`, in C.

    A SINGLE-type column (decimal, date, integer: nearly every non-text column) goes through the
    type's converter, in `map`: the SAME text as `texto(v)`, without per-value type comparisons.
    Mixed types go through `texto`."""
    tipos = set(map(type, valores))
    tem_nulo = type(None) in tipos
    tipos.discard(type(None))
    if tipos <= {str}:
        return valores
    if len(tipos) == 1 and (conv := _CONVERSOR_POR_TIPO.get(next(iter(tipos)))) is not None:
        if not tem_nulo:
            return list(map(conv, valores))
        return [None if v is None else conv(v) for v in valores]
    return [texto(v) for v in valores]


def transpor(linhas: list, cols_ok: list[str]) -> list:
    """Batch of rows (a dict per row, as the driver delivers) -> one sequence per column, in the order
    of `cols_ok`. The transposition is `zip(*map(itemgetter(...)))`, in C; a column the row lacks
    becomes None, like `r.get(c)` in the classic path."""
    try:
        pega = itemgetter(*cols_ok)
        if len(cols_ok) == 1:
            return [list(map(pega, linhas))]
        return list(zip(*map(pega, linhas), strict=True))
    except KeyError:
        return [[r.get(c) for r in linhas] for c in cols_ok]


def lote_para_arrow(linhas: list, cols_ok: list[str]):
    """A cursor batch -> Arrow `RecordBatch`, every column `string`."""
    import pyarrow as pa

    arrays = [pa.array(coluna_texto(col), type=pa.string()) for col in transpor(linhas, cols_ok)]
    return pa.RecordBatch.from_arrays(arrays, names=cols_ok)


def lote_tuplas_para_arrow(linhas: list, indices: list[int], cols_ok: list[str]):
    """A batch of cursor TUPLES -> Arrow `RecordBatch`, every column `string`. `indices[k]` is the
    position, in the tuple, of column `cols_ok[k]`. With no dropped or repeated column, the
    transposition is plain `zip(*linhas)`."""
    import pyarrow as pa

    if indices == list(range(len(indices))) and linhas and len(linhas[0]) == len(indices):
        colunas = list(zip(*linhas, strict=True))
    elif len(indices) == 1:
        colunas = [[r[indices[0]] for r in linhas]]
    else:
        pega = itemgetter(*indices)
        colunas = list(zip(*map(pega, linhas), strict=True))
    arrays = [pa.array(coluna_texto(col), type=pa.string()) for col in colunas]
    return pa.RecordBatch.from_arrays(arrays, names=cols_ok)


def tabela_de_lotes(lotes: list, cols_ok: list[str]):
    import pyarrow as pa

    schema = pa.schema([(c, pa.string()) for c in cols_ok])
    return pa.Table.from_batches(lotes, schema=schema)
