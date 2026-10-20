"""Reading a published dataset (silver/gold) BY RUN.

`resolver_publicado` (leitura.py) answers "what is valid NOW": it reads `_atual.json`, which the
next run of the same scope moves. Closing/reporting needs the other question - "what did THIS run
publish" - and it cannot depend on the pointer: between dispatch and read another run of the same
unit (a reload, a retry, the chain of another closing) will already have moved it, and the read
would return another run's data without any error.

The resolution is deterministic from the run, and no step is a glob or a pointer:

1. `execucoes(id)` -> pipeline, EXECUTED version and the parameters as the engine used them when
   publishing (normalised and redacted - it is the same dictionary, see
   `engine.preparar`/`_publicar_saida`).
2. The definition OF THAT version -> the `saida` node with `destino: lake`, the requested layer and
   dataset -> `conexao_id` and `particao`. Today's version may have changed the partition; the
   run's does not change.
3. The parameter segments and the index prefix come from the SAME engine functions
   (`_particao_de_parametro`, `_prefixo_imutavel`) - rewriting the rule here would create a second
   truth about where the index lives, and the two would diverge on the first adjustment to either.
   With a partition, it reads `<prefix>/execucao_id=<uuid>/_indice.json` (immutable); without one,
   the `_manifesto.json` next to the run `dados.parquet`.
4. A budgeted DuckDB (`duck.conectar("preview")`) in a thread, over the EXACT LIST of parquets.

Filtering, ordering and aggregation belong to whoever READS, they are not a transformation of the
published data: the filter value ALWAYS goes by bind (`?`), the column is checked against the real
schema and quoted, and the sum is in DECIMAL - `sum(DOUBLE)` is not deterministic (the same query
gives different results depending on thread order) and a total that changes between two clicks is
worse than no total. For the same reason the PAGE is deterministic: every read ends in (file, row in
file), because `LIMIT/OFFSET` over a tied key let the parallel Top-N pick other tied rows on each
page.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import duckdb
from sqlalchemy import text

from .. import duck
from ..errors import DetalheErro, ErroAplicacao
from ..execucao.bloqueante import em_thread
from ..io_lake import em_thread_lake
from ..periodos import resolver_corte
from ..pipelines.definicao import parametros_declarados
from ..timezone import para_exibicao
from . import armazenamento, leitura
from .familia import nome_vigente

# CLOSED list. `landing`/`bronze`/`quarantine` are the ingestion source evidence and have their own
# read path, through the batch index (`ingestao_lotes`); `relatorios` is served by `/relatorios`,
# with the operator RBAC. Opening any of them here would be a second door, with another access rule,
# to the same object.
CAMADAS_SERVIDAS = ("silver", "gold")
TAKE_PADRAO = 100
TAKE_MAXIMO = 1000
# Closed aggregation vocabulary, each function with a declared consumer: `sum` checks a report
# total; `count_distinct` counts distinct keys, which a thin client cannot count by itself (all
# calculation lives in cFlow). `avg`/`max` silently accepted and without effect would be worse than
# refused.
AGREGACOES = ("sum", "count_distinct")
# Aggregate of an EMPTY set, per function - the SQL semantics: the sum of nothing is NULL (there is
# no total to state), the count of nothing is 0 (it is a measurement). Holds equally with and without
# a selected file.
_AGREGADO_VAZIO = {"sum": None, "count_distinct": "0"}
# Page tie-breaker. `filename` accepts its own name (and wins one that no dataset uses);
# `file_row_number` does not - DuckDB refuses to rename it and fails with a binder error if the file
# has a column of the same name, so that column is refused by NAME before the query.
_COL_ARQUIVO = "__cflow_arquivo"
_COL_LINHA = "file_row_number"
_RESERVADAS = frozenset({_COL_ARQUIVO, _COL_LINHA})
_LEITURA_PAGINA = (
    f"read_parquet(?, hive_partitioning = false, filename = '{_COL_ARQUIVO}', "
    f"file_row_number = true)"
)
# Types compared in their OWN type, not as text: the `CAST(... AS VARCHAR)` of a TIMESTAMP uses a
# space and the response comes out in `isoformat()` with `T`; that of a TIME trims the zeros
# (`12:17:23.5`) and the response carries `12:17:23.500000` - in both the value copied from the row
# did not match.
_TEMPORAL = re.compile(r"^(DATE|TIME|TIME WITH TIME ZONE|TIMESTAMP.*)$")
# Types that the DuckDB-to-Python conversion does NOT deliver as they are, and so leave the page
# through their own expression (`_projecao`): TIMESTAMPTZ requires `pytz`, which the product does not
# have - the whole page fell into a 500 -, and TIMESTAMP_NS is cut at the microsecond, so the
# response was born different from the data and the value copied from it matched nothing.
_COM_FUSO = "TIMESTAMP WITH TIME ZONE"
_NANO = "TIMESTAMP_NS"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_INTEIRO_TEXTO = re.compile(r"^-?\d+$")
# Same cap as the publication fan-out (`engine._CONCORRENCIA_PARTICAO`): on GCS, each partition is a
# download to the cache; 190 units in series is waiting on the network, with no cap it is a memory
# and socket spike.
_CONCORRENCIA_MATERIALIZAR = 8

_MES = re.compile(r"^\d{4}-\d{2}$")
_DECIMAL = re.compile(r"^DECIMAL\((\d+),\s*(\d+)\)$")
_INTEIROS = frozenset(
    {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "HUGEINT",
        "UTINYINT",
        "USMALLINT",
        "UINTEGER",
        "UBIGINT",
        "UHUGEINT",
    }
)


# --- route parameters (pure) -------------------------------------------------------------------


def validar_paginacao(skip: int, take: int) -> None:
    if skip < 0 or not 1 <= take <= TAKE_MAXIMO:
        raise ErroAplicacao(
            codigo="paginacao_invalida",
            mensagem=f"Invalid pagination: skip must be >= 0 and take between 1 and {TAKE_MAXIMO}.",
            status=400,
        )


def interpretar_filtros(brutos: list[str]) -> list[tuple[str, str, str]]:
    """`column=value` (equality over the text) and `column~text` (contains, case-insensitive).

    Splits at the FIRST operator: the value is free text and may contain `=` and `~`; the column
    name may not. Splitting at the last one would send part of the value into the column name."""
    saida: list[tuple[str, str, str]] = []
    for bruto in brutos:
        posicoes = [i for i in (bruto.find("="), bruto.find("~")) if i >= 0]
        if not posicoes or min(posicoes) == 0:
            raise ErroAplicacao(
                codigo="filtro_invalido",
                mensagem=(
                    f"Invalid filter {bruto!r}: use 'column=value' (equals) or 'column~text' "
                    "(contains)."
                ),
                status=400,
            )
        i = min(posicoes)
        saida.append((bruto[:i], bruto[i], bruto[i + 1 :]))
    return saida


def interpretar_ordenar(brutos: list[str]) -> list[tuple[str, bool]]:
    """`column` (ascending) or `-column` (descending) -> (column, desc)."""
    saida: list[tuple[str, bool]] = []
    for bruto in brutos:
        desc = bruto.startswith("-")
        coluna = bruto[1:] if desc else bruto
        if not coluna:
            raise ErroAplicacao(
                codigo="ordenacao_invalida",
                mensagem=f"Invalid ordering {bruto!r}: use 'column' or '-column'.",
                status=400,
            )
        saida.append((coluna, desc))
    return saida


def interpretar_agregar(brutos: list[str]) -> list[tuple[str, str]]:
    """`sum:Col1,Col2` and `count_distinct:Col3` (repeatable, combinable) ->
    [("sum", "Col1"), ("sum", "Col2"), ("count_distinct", "Col3")]."""
    saida: list[tuple[str, str]] = []
    for bruto in brutos:
        funcao, sep, lista = bruto.partition(":")
        colunas = lista.split(",") if sep else []
        if funcao not in AGREGACOES or not colunas or any(not c for c in colunas):
            raise ErroAplicacao(
                codigo="agregacao_invalida",
                mensagem=(
                    f"Invalid aggregation {bruto!r}: use '<function>:Column1,Column2' "
                    f"(accepted functions: {', '.join(AGREGACOES)})."
                ),
                status=400,
            )
        saida.extend((funcao, c) for c in colunas)
    return saida


def valor_json(v):
    """Cell value -> JSON without silent loss.

    DECIMAL becomes TEXT (`format 'f'`, never scientific notation: `0E-10` comes out
    `0.0000000000`) - the response serialiser would turn a `Decimal` into a float and eat the digit
    that a zero-cent reconciliation comes looking for. NaN/inf become `null`: JSON cannot represent
    them and the default `json.dumps` would emit a `NaN` that no strict client reads."""
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, datetime) and v.tzinfo is not None:
        # An instant with a timezone: comes out in the display timezone with the offset OF THAT
        # instant (daylight saving included), the same convention as the API moments.
        return para_exibicao(v).isoformat()
    if isinstance(v, datetime | date | time):
        return v.isoformat()
    if isinstance(v, timedelta):
        return str(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, bytes | bytearray | memoryview):
        return base64.b64encode(bytes(v)).decode()
    if isinstance(v, dict):
        return {str(k): valor_json(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [valor_json(x) for x in v]
    return v


# --- index partitions (pure) -------------------------------------------------------------------


def _casa(guardado, pedido) -> bool:
    """Same text the path uses (`texto_particao`). And `2026-08` matches `2026-08-01`: the competence
    is published as DAY 1 (the parameter arrives expanded in `_inicio`), but whoever asks thinks in
    months. The equivalence is declared, not a prefix: `2026-08` does not match `2026-08-15`."""
    g = leitura.texto_particao(guardado)
    p = leitura.texto_particao(pedido)
    return g == p or (bool(_MES.match(p)) and g == f"{p}-01")


def _instante(valor) -> datetime | None:
    """Instant of an `as_of`, through the SAME function that normalised it in the run
    (`resolver_corte`): `T` or space, with or without fraction, and a bare day becomes the end of the
    day - exactly what the run would have received. With a timezone, converts to the display clock
    (the persisted `as_of` carries no timezone). None = not an instant."""
    if isinstance(valor, datetime):
        dt = valor
    else:
        texto = valor.isoformat() if isinstance(valor, date) else str(valor)
        try:
            dt = datetime.fromisoformat(resolver_corte(texto))
        except ValueError:
            return None
    return para_exibicao(dt).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _casa_parametro(chave: str, guardado, pedido: str, tipo: str | None) -> bool:
    """Check a run parameter that does NOT partition the dataset, by its declared TYPE.

    `texto_particao` cuts every `YYYY-MM-DD HH:MM...` at the day because it serves a PATH SEGMENT,
    which is already at the day. Here the value is that of the whole run: `as_of` is the instant of
    the snapshot, and two runs of the same day with different as-ofs are different executions -
    cutting at the day returned the 12:17:23 run to whoever asked for 23:59:59, with no error.
    `data` is a day by definition; `competencia` keeps the month equivalence; the rest is the text as
    it came. Without a type map, the previous heuristic."""
    if tipo is None or tipo == "competencia":
        return _casa(guardado, pedido)
    if tipo == "data":
        return leitura.texto_particao(guardado) == leitura.texto_particao(pedido)
    if tipo == "as_of":
        alvo = _instante(pedido)
        if alvo is None:
            raise ErroAplicacao(
                codigo="filtro_particao_invalido",
                mensagem=(
                    f"'{chave}' is an as-of parameter of the run: {pedido!r} is not a date/time. "
                    "Use ISO, as the run records it (e.g. 2026-08-06T12:17:23)."
                ),
                status=400,
            )
        corrida = _instante(guardado)
        if corrida is not None:
            return corrida == alvo
    return str(guardado).strip() == str(pedido).strip()


def _valor_do_parametro(parametros: dict, chave: str, tipos_parametro: dict | None):
    """Value the run executed with. A `competencia` parameter does not exist raw - validation expands
    it into `<name>_inicio`/`<name>_fim` (definicao.py) -, and the start is day 1 of the competence.

    Only the TYPE authorises falling back to `_inicio`, through the SAME engine function
    (`_competencia_expandida`): without it, `data` inherited the value of a `data_inicio` of type
    `data`, and a filter that should be a 400 returned the whole run - a rule the engine had already
    closed stayed open in the reader."""
    from ..execucao import engine  # late: the engine imports `ingestao` (cycle)

    valor = parametros.get(chave)
    if valor in (None, "") and engine._competencia_expandida(chave, parametros, tipos_parametro):
        valor = parametros.get(f"{chave}_inicio")
    return None if valor in (None, "") else valor


def _ausente(dataset: str, camada: str, execucao_id, detalhe: str) -> ErroAplicacao:
    return ErroAplicacao(
        codigo="publicado_ausente",
        mensagem=(
            f"Run {execucao_id} has no dataset '{dataset}' published in layer '{camada}': {detalhe}"
        ),
        status=404,
    )


def selecionar_particoes(
    particoes: list[dict],
    filtros: dict[str, str],
    *,
    base: dict[str, str],
    chaves_coluna: list[str],
    parametros: dict,
    dataset: str,
    execucao_id,
    tipos_parametro: dict | None = None,
) -> list[dict]:
    """The index partitions the request covers. `tipos_parametro` is the map of the EXECUTED version
    (`saida_publicada`); without it the engine heuristic applies (both sides of the window).

    - COLUMN key (e.g. project): filters the partitions. No match is a legitimate answer - the run
      did not publish that project - and comes out as zero rows.
    - PARAMETER key (e.g. unit, competence): constant in the run, so it is CHECKED against it. A
      different value means asking the wrong run, and answering "zero rows" would be the plausible
      and wrong result: a 404 naming what the run published.
    - A run parameter that does not partition the dataset: the same check (the whole run has that
      value), but by the declared TYPE (`_casa_parametro`) - the as-of by the instant.
    - Any other key: 400. Accepting it silently would return the WHOLE run to whoever asked for a
      slice of it."""
    selecionadas = list(particoes)
    for chave, pedido in filtros.items():
        if chave in chaves_coluna:
            selecionadas = [
                p for p in selecionadas if _casa((p.get("particao") or {}).get(chave), pedido)
            ]
            continue
        if chave in base:
            guardado, tipo = base[chave], None
        else:
            guardado = _valor_do_parametro(parametros, chave, tipos_parametro)
            tipo = (tipos_parametro or {}).get(chave)
        if guardado is None:
            chaves = [*base, *chaves_coluna]
            raise ErroAplicacao(
                codigo="filtro_particao_invalido",
                mensagem=(
                    f"'{chave}' is not a partition key of dataset '{dataset}' "
                    f"(keys: {', '.join(chaves) or 'none'}) nor a parameter with a value in this "
                    f"run. To filter by content, use filtro={chave}=<value>."
                ),
                status=400,
            )
        if not (
            _casa(guardado, pedido)
            if chave in base
            else _casa_parametro(chave, guardado, pedido, tipo)
        ):
            # The run value as it used it: the as-of with the time - saying only the day would make
            # "published with X, not Y" look the same on both sides.
            exibido = (
                leitura.texto_particao(guardado)
                if tipo in (None, "data", "competencia")
                else str(guardado)
            )
            raise ErroAplicacao(
                codigo="publicado_ausente",
                mensagem=(
                    f"Run {execucao_id} published '{dataset}' with "
                    f"{chave}={exibido}, not {chave}={pedido}. For another "
                    "scope, read the run that produced it."
                ),
                status=404,
            )
    return selecionadas


# --- DuckDB query (synchronous; runs in a thread) ----------------------------------------------


def _abrir_duck():
    """BUDGETED connection of the `preview` profile (runs in the API). A function of its own so the
    test can spy on the text of every SQL executed - proof that the filter value never enters it.

    SESSION timezone set explicitly: it is the one in which a filter without offset on a TIMESTAMPTZ
    column is read (the response comes out in the display timezone). With the default, the same
    `2026-08-06T12:17:23` would be 12:17 local on a developer machine and 12:17 UTC in the container
    - 3 h apart, with no error."""
    con = duck.conectar("preview")
    con.execute("SET TimeZone = 'America/Sao_Paulo'")
    return con


def _citar(coluna: str) -> str:
    return '"' + coluna.replace('"', '""') + '"'


def _escapar_like(valor: str) -> str:
    """`%` and `_` in the value are LITERALS: whoever filters by "50%" wants the text "50%", not "50"
    followed by anything."""
    return valor.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _resolver_coluna(nome: str, reais: list[str]) -> str:
    """Exact name; failing that, the only one that matches ignoring case (an upstream system may
    write `PROJECTID` while whoever types writes `projectid`). Missing or ambiguous is a 400 NAMING
    the column."""
    if nome in reais:
        return nome
    por_caixa = [c for c in reais if c.lower() == nome.lower()]
    if len(por_caixa) == 1:
        return por_caixa[0]
    raise ErroAplicacao(
        codigo="coluna_inexistente",
        mensagem=(
            f"The dataset has no column '{nome}'. Available columns: "
            f"{', '.join(reais) or '(none)'}."
        ),
        status=400,
    )


def _escala_da_soma(tipo: str) -> int:
    """Scale of the DECIMAL in which the column is summed. DECIMAL keeps ITS OWN (a sum of cents comes
    out in cents); integers sum with no decimals; DOUBLE/numeric text enter with 10 places - each
    value is rounded ONCE, always the same way, and the DECIMAL sum is exact, so the total does not
    depend on thread order. DECIMAL(38, 10) still leaves 28 integer digits."""
    m = _DECIMAL.match(tipo)
    if m:
        return int(m.group(2))
    return 0 if tipo in _INTEIROS else 10


def _por_funcao(agregados: list[tuple[str, str]], resolver) -> dict[str, dict[str, str]]:
    """{function: {REQUESTED name: real name}} in the requested order, without repeating a request
    inside the function. Grouping BY FUNCTION is what lets the same column be summed and counted: a
    flat `{column: value}` map would keep only the last.

    The response key is the REQUESTED name; the real one only goes to the SQL. Whoever asks for
    `count_distinct:saleid` reads `agregados['count_distinct']['saleid']` - returning it under
    `SaleId` (the file spelling, which the resolver found ignoring case) made the client miss the
    total, and the key even changed depending on whether the run had a file or not."""
    saida: dict[str, dict[str, str]] = {}
    for funcao, coluna in agregados:
        colunas = saida.setdefault(funcao, {})
        if coluna not in colunas:
            colunas[coluna] = resolver(coluna)
    return saida


def _agregados_vazios(por_funcao: dict[str, dict[str, str]]) -> dict:
    return {f: {pedido: _AGREGADO_VAZIO[f] for pedido in cols} for f, cols in por_funcao.items()}


def _expr_agregado(funcao: str, coluna: str, tipo: str) -> str:
    if funcao == "sum":
        return f"sum(CAST({_citar(coluna)} AS DECIMAL(38, {_escala_da_soma(tipo)})))"
    # NULL is not counted: it is not a value.
    return f"count(DISTINCT {_citar(coluna)})"


def _condicao(con, coluna: str, tipo: str, op: str, valor: str) -> tuple[str, str]:
    """(condition SQL, bind value). The value NEVER enters the text; the interpolated type comes
    from the file own DESCRIBE and went through the `_TEMPORAL` allowlist."""
    if op == "=" and _TEMPORAL.match(tipo):
        # Checked BEFORE: a `TRY_CAST` that fails becomes NULL and the query would return zero rows -
        # "does not exist" in place of "wrong request", the plausible and wrong result.
        if con.execute(f"SELECT TRY_CAST(? AS {tipo}) IS NULL", [valor]).fetchone()[0]:
            raise ErroAplicacao(
                codigo="filtro_invalido",
                mensagem=(
                    f"Filter on '{coluna}' ({tipo}): {valor!r} is not a valid date/instant. "
                    "Use ISO, as the response itself returns it (e.g. 2026-08-06 or "
                    "2026-08-06T12:17:23)."
                ),
                status=400,
            )
        return f"{_citar(coluna)} = TRY_CAST(? AS {tipo})", valor
    ref = f"CAST({_citar(coluna)} AS VARCHAR)"
    if op == "=":
        return f"{ref} = ?", valor
    return f"{ref} ILIKE ? ESCAPE '\\'", f"%{_escapar_like(valor)}%"


def _rotulo(funcao: str, coluna: str) -> str:
    return f"sum '{coluna}'" if funcao == "sum" else f"count the distinct values of '{coluna}'"


def _recusar_reservadas(reais: list[str]) -> None:
    reservadas = [c for c in reais if c.lower() in _RESERVADAS]
    if reservadas:
        raise ErroAplicacao(
            codigo="coluna_reservada",
            mensagem=(
                f"The dataset has the column {', '.join(repr(c) for c in reservadas)}, a name this "
                "read reserves for the stable page order (file and row). "
                "Rename it in the pipeline output node."
            ),
            status=409,
        )


def _projecao(coluna: str, tipo: str) -> str:
    """The column as it comes out on the PAGE. Filter and order stay over the real column (`t.`):
    the alias has the same name, and DuckDB resolves `ORDER BY "col"` by the alias - a TIMESTAMPTZ
    ordered by the epoch text would put 2001 (15 digits) after 2026 (16).

    - TIMESTAMPTZ: the epoch in microseconds, as text, which `_celula` converts to the display
      timezone without `pytz`. Out of Python `datetime` range (infinity, year > 9999) the DuckDB
      text remains.
    - TIMESTAMPTZ inside STRUCT/LIST/MAP: the DuckDB text (in the session timezone). The nested
      conversion would also need `pytz`, and text is better than a 500 page.
    - TIMESTAMP_NS: the DuckDB text, which has the nine digits (with `T`, like `isoformat()`)."""
    ref = f"t.{_citar(coluna)}"
    if tipo == _COM_FUSO:
        expr = (
            f"CASE WHEN {ref} BETWEEN TIMESTAMPTZ '0001-01-01 00:00:00+00' "
            f"AND TIMESTAMPTZ '9999-12-31 23:59:59.999999+00' "
            f"THEN CAST(epoch_us({ref}) AS VARCHAR) ELSE CAST({ref} AS VARCHAR) END"
        )
    elif _COM_FUSO in tipo:
        expr = f"CAST({ref} AS VARCHAR)"
    elif tipo == _NANO:
        expr = f"replace(CAST({ref} AS VARCHAR), ' ', 'T')"
    else:
        expr = ref
    return f"{expr} AS {_citar(coluna)}"


def _celula(valor, tipo: str):
    if tipo == _COM_FUSO and isinstance(valor, str) and _INTEIRO_TEXTO.match(valor):
        return valor_json(_EPOCH + timedelta(microseconds=int(valor)))
    return valor_json(valor)


def _agregar(con, pares: list[tuple[str, str, str]], tipos: dict, onde: str, binds: list):
    """The aggregates in a single query. Only a DATA failure becomes a 400 -
    `ConversionException`/`OutOfRangeException`, the subclasses of `DataError`, which is the sum CAST
    to DECIMAL refusing a value; `count(DISTINCT)` converts nothing. Out of memory, a corrupted
    cache file and interruption are INFRASTRUCTURE and propagate (`consultar` names the out of
    memory): calling them "a value that is not a number" would tell the client to fix a request that
    is correct.

    With several columns in the same SELECT, DuckDB does not say which one failed to convert. Only on
    this error path, they are redone one by one to name the culprit - blaming the valid column sends
    people to check the wrong data, which is what the old message did."""

    def _sql(ps):
        expr = ", ".join(_expr_agregado(f, real, tipos[real]) for f, _, real in ps)
        return f"SELECT {expr} FROM read_parquet(?, hive_partitioning = false) t{onde}"

    try:
        return con.execute(_sql(pares), binds).fetchone()
    except duckdb.DataError:
        culpadas = []
        for par in pares:
            try:
                con.execute(_sql([par]), binds).fetchone()
            except duckdb.DataError:
                culpadas.append(par)
        raise ErroAplicacao(
            codigo="agregacao_invalida",
            mensagem=(
                f"Could not {', '.join(_rotulo(f, p) for f, p, _ in culpadas or pares)}: "
                "there is a value that is not a number or that exceeds DECIMAL(38)."
            ),
            status=400,
        ) from None


def _sem_arquivo(
    colunas_esquema: list[str] | None,
    *,
    filtros: list[tuple[str, str, str]],
    ordem: list[tuple[str, bool]],
    agregados: list[tuple[str, str]],
) -> dict:
    """No parquet selected and none to read the schema from: the run index has no partition
    (column partitioning + `permitir_vazio` with no rows at all). The schema comes from the index
    `colunas` - names only, so `tipo` comes out `null` -, and every requested column is checked
    against it as it would be against the file. Without that list the answer is 409: a 200 with
    `colunas: []` would accept filter, order and aggregate on a column that does not exist, and "no
    rows" would be indistinguishable from "dataset without schema"."""
    if not colunas_esquema:
        raise ErroAplicacao(
            codigo="esquema_indisponivel",
            mensagem=(
                "There is no published file nor a column list in the index to read the dataset "
                "schema from: filter, ordering and aggregation cannot be checked."
            ),
            status=409,
        )
    reais = [str(c) for c in colunas_esquema]
    _recusar_reservadas(reais)
    for coluna, _, _ in filtros:
        _resolver_coluna(coluna, reais)
    for coluna, _ in ordem:
        _resolver_coluna(coluna, reais)
    por_funcao = _por_funcao(agregados, lambda c: _resolver_coluna(c, reais))
    return {
        "colunas": [{"nome": c, "tipo": None} for c in reais],
        "linhas": [],
        "total": 0,
        "agregados": _agregados_vazios(por_funcao),
    }


def consultar(
    caminhos: list[str],
    *,
    filtros: list[tuple[str, str, str]],
    ordem: list[tuple[str, bool]],
    agregados: list[tuple[str, str]],
    skip: int,
    take: int,
    caminho_esquema: str | None = None,
    colunas_esquema: list[str] | None = None,
) -> dict:
    """Page + total + sum over the UNION of the given parquets - the exact list, never a glob.

    `caminho_esquema` is used when the partition filter left no file at all: the response still says
    which columns the dataset has (and checks the requested ones), otherwise "no rows" and "dataset
    without schema" would be indistinguishable. When even that does not exist (an index with no
    partition at all), `colunas_esquema` applies - the list of names the run index records; without
    both, 409 (`_sem_arquivo`). Every requested column is resolved BEFORE the first data query: a
    name error comes out as 400, not halfway through.

    `hive_partitioning = false` in EVERY read: the object lives at
    `.../project_id=1201/execucao_id=<uuid>/dados.parquet` and DuckDB auto-detects hive partitioning
    from the path - `project_id` and `execucao_id` would appear as columns the file does not have
    (the partition column is already INSIDE the parquet), and a filter over them would change
    meaning without warning.

    The page ALWAYS ends in (file, row in file), after the requested columns - and it is the whole
    order when nothing is requested. Each page is an independent query with `LIMIT/OFFSET`, and over
    a tied key the parallel Top-N of DuckDB picks another subset of the tied rows each time
    (measured: up to 382 repeated rows over 5 pages and the same number missing, with no error, while
    `total` said everything was there). The file list comes from the run IMMUTABLE index, so the
    tie-break does not change between two reads either.

    `agregados` comes out BY FUNCTION and under the REQUESTED name - `{"sum": {col: "<decimal>"},
    "count_distinct": {col: "<integer>"}}`, all as text - and only with the requested functions
    (`{}` when none).

    A DuckDB out-of-memory (the budget of the `preview` profile is split among the API queries) is a
    503 `consulta_sem_memoria`: it is capacity, not a wrong request. The other DuckDB failures that
    are not data failures (corrupted file, interruption) propagate as infrastructure failures."""
    fonte = caminhos or ([caminho_esquema] if caminho_esquema else [])
    if not fonte:
        return _sem_arquivo(colunas_esquema, filtros=filtros, ordem=ordem, agregados=agregados)
    con = _abrir_duck()
    try:
        esquema = con.execute(
            "DESCRIBE SELECT * FROM read_parquet(?, hive_partitioning = false)", [fonte]
        ).fetchall()
        colunas = [{"nome": str(r[0]), "tipo": str(r[1])} for r in esquema]
        reais = [c["nome"] for c in colunas]
        tipos = {c["nome"]: c["tipo"] for c in colunas}
        _recusar_reservadas(reais)

        # Repeated equality on the SAME column is "equals any" (`project=A` + `project=B` -> A or B);
        # the rest combine with AND. With AND, the two equalities would always be zero rows - it is
        # the request of whoever filters several projects at once.
        grupos: dict[object, tuple[list[str], list[str]]] = {}
        for i, (coluna, op, valor) in enumerate(filtros):
            real = _resolver_coluna(coluna, reais)
            condicao, bind = _condicao(con, real, tipos[real], op, valor)
            conds, bs = grupos.setdefault((real, op) if op == "=" else i, ([], []))
            conds.append(condicao)
            bs.append(bind)
        condicoes: list[str] = []
        binds: list[str] = []
        for conds, bs in grupos.values():
            condicoes.append(conds[0] if len(conds) == 1 else f"({' OR '.join(conds)})")
            binds.extend(bs)
        desempate = [f"t.{_citar(_COL_ARQUIVO)} ASC", f"t.{_COL_LINHA} ASC"]
        ordem_sql = ", ".join(
            [
                *(
                    f"t.{_citar(_resolver_coluna(c, reais))} {'DESC' if desc else 'ASC'}"
                    for c, desc in ordem
                ),
                *desempate,
            ]
        )
        por_funcao = _por_funcao(agregados, lambda c: _resolver_coluna(c, reais))
        if not caminhos:
            return {
                "colunas": colunas,
                "linhas": [],
                "total": 0,
                "agregados": _agregados_vazios(por_funcao),
            }

        onde = f" WHERE {' AND '.join(condicoes)}" if condicoes else ""
        total = con.execute(
            f"SELECT count(*) FROM read_parquet(?, hive_partitioning = false) t{onde}",
            [caminhos, *binds],
        ).fetchone()[0]
        projecao = ", ".join(_projecao(c["nome"], c["tipo"]) for c in colunas)
        cursor = con.execute(
            f"SELECT {projecao} FROM {_LEITURA_PAGINA} t{onde} ORDER BY {ordem_sql} "
            "LIMIT ? OFFSET ?",
            [caminhos, *binds, take, skip],
        )
        nomes = [d[0] for d in cursor.description]
        linhas = [
            {n: _celula(v, tipos.get(n, "")) for n, v in zip(nomes, linha, strict=True)}
            for linha in cursor.fetchall()
        ]

        resultado: dict[str, dict[str, str | None]] = {}
        pares = [
            (f, pedido, real) for f, cols in por_funcao.items() for pedido, real in cols.items()
        ]
        if pares:
            valores = _agregar(con, pares, tipos, onde, [caminhos, *binds])
            for (funcao, pedido, _), v in zip(pares, valores, strict=True):
                texto = None if v is None else (str(v) if isinstance(v, int) else valor_json(v))
                resultado.setdefault(funcao, {})[pedido] = texto
        return {"colunas": colunas, "linhas": linhas, "total": int(total), "agregados": resultado}
    except duckdb.OutOfMemoryException as e:
        raise ErroAplicacao(
            codigo="consulta_sem_memoria",
            mensagem=(
                "The read exceeded the memory reserved for queries in this API. Narrow the slice "
                "(filter or partition) or try again later."
            ),
            status=503,
        ) from e
    finally:
        con.close()


# --- resolution (database + lake) --------------------------------------------------------------


@dataclass(frozen=True)
class Publicacao:
    """What ONE run published for ONE dataset, resolved from the run."""

    execucao_id: str
    dataset: str
    camada: str
    lake: armazenamento.Armazenamento
    base: dict[str, str]  # PARAMETER partition segments (constant in the run)
    chaves_coluna: list[str]  # COLUMN partition segments (what the index enumerates)
    parametros: dict
    documento: dict  # `_indice.json` or `_manifesto.json`, as it is in the lake
    particoes: list[dict] = field(default_factory=list)  # {particao, caminho, linhas, bytes}
    # Parameter types in the EXECUTED version: they tell whether `competencia` answers through
    # `_inicio` in a parameter filter that does not partition (same rule as the engine).
    tipos_parametro: dict | None = None


def exigir_camada(camada: str) -> None:
    if camada not in CAMADAS_SERVIDAS:
        raise ErroAplicacao(
            codigo="nao_encontrado",
            mensagem=(
                f"Layer '{camada}' is not served by this read (only {', '.join(CAMADAS_SERVIDAS)})."
            ),
            status=404,
        )


def _json(valor):
    return json.loads(valor) if isinstance(valor, str) else valor


async def carregar_execucao(conn, execucao_id) -> dict:
    """Step 1. Resolve the pipeline run, with the parameters persisted by the engine."""
    row = (
        await conn.execute(
            text("SELECT id, pipeline_id, versao_numero, parametros FROM execucoes WHERE id = :id"),
            {"id": execucao_id},
        )
    ).first()
    if row is None:
        raise ErroAplicacao(codigo="nao_encontrado", mensagem="Run not found.", status=404)
    return {
        "id": str(row.id),
        "pipeline_id": row.pipeline_id,
        "versao_numero": row.versao_numero,
        "parametros": _json(row.parametros) or {},
    }


#: (parameter types, [(layer, dataset, config)] of the `lake` outputs) per PUBLISHED VERSION, per
#: process. The `publicado` source in the chain re-read and decoded the producer whole definition
#: for every reader node. The published row only changes through `atualizar_layout` (node
#: positions), which nothing here reads; drafts are not cached - they change without changing
#: number. The cap only keeps the dictionary from growing without limit in a long-lived worker.
_RESUMO_POR_VERSAO: dict[tuple[str, object], tuple[dict, list]] = {}
_TETO_RESUMOS = 1024


async def _resumo_da_versao(conn, pipeline_id, numero) -> tuple[dict, list]:
    chave = (str(pipeline_id), numero)
    resumo = _RESUMO_POR_VERSAO.get(chave)
    if resumo is not None:
        return resumo
    linha = (
        await conn.execute(
            text(
                "SELECT definicao, status FROM pipeline_versoes "
                "WHERE pipeline_id = :p AND numero = :n"
            ),
            {"p": pipeline_id, "n": numero},
        )
    ).first()
    definicao = (_json(linha.definicao) if linha is not None else None) or {}
    tipos = {
        p["nome"]: p.get("tipo")
        for p in parametros_declarados(definicao)
        if isinstance(p, dict) and p.get("nome")
    }
    saidas = []
    for no in definicao.get("nos") or []:
        cfg = no.get("config") or {}
        if no.get("tipo") == "saida" and str(cfg.get("destino") or "arquivo").lower() == "lake":
            saidas.append((str(cfg.get("camada") or "silver"), cfg.get("dataset"), cfg))
    resumo = (tipos, saidas)
    if linha is not None and str(linha.status) == "publicada":
        if len(_RESUMO_POR_VERSAO) >= _TETO_RESUMOS:
            _RESUMO_POR_VERSAO.clear()
        _RESUMO_POR_VERSAO[chave] = resumo
    return resumo


async def saida_publicada(conn, execucao: dict, camada: str, dataset: str) -> dict:
    """Step 2. The `saida` node of the EXECUTED VERSION that publishes `dataset` in `camada` -> its
    config.

    From the executed version, never the latest: the partition (and with it the index path) belongs
    to the definition that ran. Called AFTER authorisation, so the named 404 does not describe the
    graph of a pipeline the caller cannot see."""
    tipos, saidas = await _resumo_da_versao(
        conn, execucao["pipeline_id"], execucao["versao_numero"]
    )
    # The type map of the EXECUTED VERSION, the same the engine handed to `_publicar_saida`: it
    # decides whether `competencia` enters the path through `_inicio`. Without it the resolution
    # would fall back to the both-sides-of-the-window heuristic and could diverge from the path the
    # publication wrote.
    execucao["tipos_parametro"] = dict(tipos)
    # By the CURRENT name on both sides: a version executed before the family reorganisation
    # published `ref01_summary`, and its data now lives at `ref/01/summary` (copied, with the index
    # rewritten, and the old layout deleted). The `dataset` in the returned config is the current
    # one, because the index path comes from it - the old one no longer exists in the lake.
    alvo = nome_vigente(dataset)
    candidatos = [
        {**cfg, "dataset": alvo} for c, d, cfg in saidas if c == camada and nome_vigente(d) == alvo
    ]
    if not candidatos:
        raise _ausente(
            dataset,
            camada,
            execucao["id"],
            f"version {execucao['versao_numero']} of the pipeline has no output node for it.",
        )
    if len(candidatos) > 1:
        # Two nodes publishing the same dataset in the same run collide on lake immutability;
        # silently choosing one would return half of a publication that did not even finish.
        raise ErroAplicacao(
            codigo="publicado_ambiguo",
            mensagem=(
                f"Version {execucao['versao_numero']} has {len(candidatos)} output nodes "
                f"publishing '{dataset}' in layer '{camada}'."
            ),
            status=409,
        )
    return candidatos[0]


async def _ler_documento(lake, camada: str, rel: str, dataset: str, execucao_id) -> dict:
    """Read the run IMMUTABLE index/manifest. Does NOT go through `ler_ponteiro`, which turns the
    404 into `lake_dataset_ausente` ("not published yet") - here the question is another: did this
    run publish? And the answer must name the missing object."""
    try:
        return json.loads(await lake.ler(camada, rel))
    except ErroAplicacao as e:
        if e.status == 404:
            raise _ausente(
                dataset,
                camada,
                execucao_id,
                f"object '{rel}' does not exist in the lake (the output node may not have "
                "published - run in progress or failure before publishing).",
            ) from None
        raise


async def abrir_publicacao(execucao: dict, cfg: dict, camada: str, dataset: str) -> Publicacao:
    """Step 3: the run document and the exact list of parquets it published."""
    from ..execucao import engine

    lake = await armazenamento.abrir(cfg.get("conexao_id"))
    eid = execucao["id"]
    ambiente = leitura.ambiente_da_camada(
        camada,
        execucao.get("parametros"),
        obrigatorio_gold=camada == "gold",
    )
    parametros = execucao["parametros"]
    cfg_particao = list(cfg.get("particao") or [])
    if cfg_particao:
        base = engine._particao_de_parametro(
            cfg_particao, parametros, tipos_parametro=execucao.get("tipos_parametro")
        )
        prefixo = engine._prefixo_imutavel(
            dataset,
            cfg_particao,
            base,
            camada=camada,
            ambiente=parametros.get("ambiente"),
        )
        rel = f"{prefixo}/execucao_id={eid}/{leitura.INDICE}"
        documento = await _ler_documento(lake, camada, rel, dataset, eid)
        leitura.validar_documento_publicado(
            documento,
            dataset=dataset,
            camada=camada,
            ambiente=ambiente,
            execucao_id=eid,
        )
        particoes = list(documento.get("particoes") or [])
    else:
        base = {}
        raiz_dataset = leitura.prefixo_dataset(dataset, ambiente)
        rel = f"{raiz_dataset}/execucao_id={eid}/{leitura.MANIFESTO}"
        documento = await _ler_documento(lake, camada, rel, dataset, eid)
        leitura.validar_documento_publicado(
            documento,
            dataset=dataset,
            camada=camada,
            ambiente=ambiente,
            particao_esperada={},
            execucao_id=eid,
        )
        particoes = [
            {
                "particao": {},
                "caminho": documento.get("caminho")
                or f"{raiz_dataset}/execucao_id={eid}/dados.parquet",
                "linhas": documento.get("linhas"),
                "bytes": documento.get("bytes"),
            }
        ]
    return Publicacao(
        execucao_id=eid,
        dataset=dataset,
        camada=camada,
        lake=lake,
        base=base,
        chaves_coluna=[
            it.get("chave") or it["nome"].lower()
            for it in cfg_particao
            if it.get("origem") == "coluna"
        ],
        parametros=parametros,
        documento=documento,
        particoes=particoes,
        tipos_parametro=execucao.get("tipos_parametro"),
    )


async def _materializar(pub: Publicacao, particoes: list[dict]) -> list[str]:
    """Local paths of the parquets (no-op on the local backend; download to the cache on GCS),
    through the SAME mechanism as `resolver_publicado`. Waits for ALL before deciding: a bare
    `gather` would return control on the first error with the siblings still downloading behind the
    response."""
    sem = asyncio.Semaphore(_CONCORRENCIA_MATERIALIZAR)

    async def _um(rel: str) -> str:
        async with sem:
            return await pub.lake.materializar(
                pub.camada, rel, leitura._cache_publicado(pub.camada, rel)
            )

    resultados = await asyncio.gather(
        *(_um(p["caminho"]) for p in particoes), return_exceptions=True
    )
    for p, r in zip(particoes, resultados, strict=True):
        if isinstance(r, ErroAplicacao) and r.status == 404:
            raise _ausente(
                pub.dataset,
                pub.camada,
                pub.execucao_id,
                f"the index points to '{p['caminho']}', which does not exist in the lake.",
            ) from None
        if isinstance(r, BaseException):
            raise r
    return [str(r) for r in resultados]


def _colunas_do_indice(pub: Publicacao) -> list[str]:
    """Schema of a run that published NO partition at all (`particoes: []` - column partitioning with
    `permitir_vazio` and zero rows): there is no parquet to read from, and the index records the
    names in `colunas`. An index written before that does not have them, and answering 200 without a
    schema would accept a column that does not exist - a 409 naming the run, so whoever reads knows
    it is the index (not the request) that needs to be rewritten."""
    colunas = pub.documento.get("colunas")
    if isinstance(colunas, list) and colunas and all(isinstance(c, str) for c in colunas):
        return colunas
    raise ErroAplicacao(
        codigo="esquema_indisponivel",
        mensagem=(
            f"Run {pub.execucao_id} published '{pub.dataset}' in layer '{pub.camada}' "
            "with no partition at all, and its index does not record the dataset columns (an index "
            "written before the schema was added to it). Without the schema, filter, ordering and "
            "aggregation cannot be checked: rerun the pipeline to rewrite the index."
        ),
        status=409,
    )


async def ler_dados(
    pub: Publicacao,
    *,
    filtros_particao: dict[str, str],
    skip: int,
    take: int,
    filtro: list[str],
    ordenar: list[str],
    agregar: list[str],
) -> dict:
    """Step 4. Every parse and every parameter refusal comes BEFORE touching the lake."""
    validar_paginacao(skip, take)
    filtros = interpretar_filtros(filtro)
    ordem = interpretar_ordenar(ordenar)
    agregados = interpretar_agregar(agregar)
    selecionadas = selecionar_particoes(
        pub.particoes,
        filtros_particao,
        base=pub.base,
        chaves_coluna=pub.chaves_coluna,
        parametros=pub.parametros,
        dataset=pub.dataset,
        execucao_id=pub.execucao_id,
        tipos_parametro=pub.tipos_parametro,
    )
    caminhos = await _materializar(pub, selecionadas)
    esquema = None
    colunas_esquema = None
    if not caminhos:
        if pub.particoes:
            esquema = (await _materializar(pub, pub.particoes[:1]))[0]
        else:
            colunas_esquema = _colunas_do_indice(pub)
    resultado = await em_thread(
        consultar,
        caminhos,
        filtros=filtros,
        ordem=ordem,
        agregados=agregados,
        skip=skip,
        take=take,
        caminho_esquema=esquema,
        colunas_esquema=colunas_esquema,
    )
    return {
        "dataset": pub.dataset,
        "camada": pub.camada,
        "execucao_id": pub.execucao_id,
        "colunas": resultado["colunas"],
        "linhas": resultado["linhas"],
        "total": resultado["total"],
        "skip": skip,
        "take": take,
        "agregados": resultado["agregados"],
        "particoes": [
            {"particao": p.get("particao") or {}, "linhas": p.get("linhas")} for p in selecionadas
        ],
    }


# --- the published FILE, whole (direct download, no paging) ------------------------------------
#
# Clients stop paging `/dados` for bulk datasets. Each page re-reads and SORTS the whole set
# (`ORDER BY ... LIMIT/OFFSET`), so scanning N pages costs N sorts - measured on a production-like
# host: ~190 thousand rows = 190 pages took about a minute and a half, and a large dataset has
# around a million rows per month. The parquet the run published is already the answer: returning
# it byte for byte costs one sequential disk read, and the schema arrives in native types instead of
# JSON.

#: Streaming response block. The response never holds the whole file in memory.
BLOCO_ARQUIVO = 1024 * 1024
TIPO_PARQUET = "application/vnd.apache.parquet"
#: sha256 per PHYSICAL file (device, inode, size, mtime), per process. The published object is
#: immutable (`execucao_id=<uuid>` in the path) and the GCS cache is written with `os.replace`, so
#: the same file identity always has the same bytes; re-reading 100 MB just for the header on every
#: repeated download is a cost with no information. The cap prevents growth.
_SHA_POR_ARQUIVO: dict[tuple[int, int, int, int], str] = {}
_TETO_SHA = 4096


def arquivo_unico(pub: Publicacao, filtros_particao: dict[str, str]) -> dict:
    """The index partition the request selects - and it must be ONE.

    The same selection as `/dados` (`selecionar_particoes`: parameter checked against the run, column
    key filtering, unknown key 400). The difference is what to do with the result: the page unions N
    parquets; the download delivers ONE file, without rewriting it. Unioning here would generate a
    new parquet - another object, with another sha, that nobody published.

    - none: 404 `publicado_ausente`, the same code as `/dados` (there it is "zero rows" when the
      column key does not match; here there is no file to deliver, and a 200 with an empty body would
      be an invalid parquet dressed as a response);
    - more than one: 409 `publicado_varios_arquivos`, saying how many and which keys to filter by."""
    selecionadas = selecionar_particoes(
        pub.particoes,
        filtros_particao,
        base=pub.base,
        chaves_coluna=pub.chaves_coluna,
        parametros=pub.parametros,
        dataset=pub.dataset,
        execucao_id=pub.execucao_id,
        tipos_parametro=pub.tipos_parametro,
    )
    if not selecionadas:
        raise _ausente(
            pub.dataset,
            pub.camada,
            pub.execucao_id,
            "no published partition matches the requested slice "
            f"({', '.join(f'{k}={v}' for k, v in filtros_particao.items()) or 'no filter'}).",
        )
    if len(selecionadas) > 1:
        chaves = ", ".join(pub.chaves_coluna) or "none"
        raise ErroAplicacao(
            codigo="publicado_varios_arquivos",
            mensagem=(
                f"The requested slice of '{pub.dataset}' in run {pub.execucao_id} has "
                f"{len(selecionadas)} published files, and the download delivers a single one. "
                f"Filter by partition (keys: {chaves}) or read the set through /dados."
            ),
            status=409,
            detalhes=[
                DetalheErro(campo="arquivos", mensagem=str(len(selecionadas))),
                *(DetalheErro(campo="chave_particao", mensagem=c) for c in pub.chaves_coluna),
            ],
        )
    return selecionadas[0]


@dataclass
class ArquivoPublicado:
    """The parquet of ONE partition, OPEN. The descriptor is the same one used for the hash and the
    response: the header sha256 describes exactly the bytes that go out, even if the cache is
    swapped midway."""

    arquivo: object  # BinaryIO open in "rb", already at the start
    tamanho: int
    linhas: int | None
    sha256: str
    particao: dict

    def fechar(self) -> None:
        self.arquivo.close()


def _sha256_do_descritor(arquivo, identidade: tuple[int, int, int, int]) -> str:
    """sha256 reading the descriptor in blocks and returning to the start. The run manifest and
    index do not store a parquet digest (only `linhas` and `bytes`), so the server computes it over
    the file it is going to send - it is what the client checks against what it wrote."""
    conhecido = _SHA_POR_ARQUIVO.get(identidade)
    if conhecido is None:
        h = hashlib.sha256()
        while bloco := arquivo.read(BLOCO_ARQUIVO):
            h.update(bloco)
        conhecido = h.hexdigest()
        if len(_SHA_POR_ARQUIVO) >= _TETO_SHA:
            _SHA_POR_ARQUIVO.clear()
        _SHA_POR_ARQUIVO[identidade] = conhecido
    arquivo.seek(0)
    return conhecido


def _abrir_e_medir(caminho: str) -> tuple[object, int, str]:
    arquivo = open(caminho, "rb")  # noqa: SIM115 - closed by `ArquivoPublicado.fechar`
    try:
        st = os.fstat(arquivo.fileno())
        sha = _sha256_do_descritor(arquivo, (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns))
        return arquivo, st.st_size, sha
    except BaseException:
        arquivo.close()
        raise


async def abrir_arquivo(pub: Publicacao, filtros_particao: dict[str, str]) -> ArquivoPublicado:
    """Step 4 of the download: the single partition -> local file (no-op on the local lake; cache in
    `lake_cache_dir`, downloaded in blocks, on GCS) -> opened, measured and with its sha256.

    The size on disk is checked against the `bytes` the publication recorded in the index/manifest:
    a divergence means the lake (or the cache) is inconsistent with what the run published, and
    serving it anyway would hand the client a file that matches itself and not the run."""
    particao = arquivo_unico(pub, filtros_particao)
    (caminho,) = await _materializar(pub, [particao])
    arquivo, tamanho, sha = await em_thread_lake(_abrir_e_medir, caminho)
    registrado = particao.get("bytes")
    if isinstance(registrado, int) and registrado != tamanho:
        arquivo.close()
        raise ErroAplicacao(
            codigo="publicado_divergente",
            mensagem=(
                f"The published file of '{pub.dataset}' in run {pub.execucao_id} has "
                f"{tamanho} bytes, and the publication recorded {registrado}. The lake (or its "
                "local cache) does not match the run; nothing was sent."
            ),
            status=502,
        )
    linhas = particao.get("linhas")
    return ArquivoPublicado(
        arquivo=arquivo,
        tamanho=tamanho,
        linhas=int(linhas) if isinstance(linhas, int) else None,
        sha256=sha,
        particao=dict(particao.get("particao") or {}),
    )


async def em_blocos(arq: ArquivoPublicado, bloco: int | None = None):
    """The file bytes in `BLOCO_ARQUIVO` blocks, read in the lake pool - never `read()` without a
    size. Closes the descriptor at the end, including when the client gives up midway."""
    tamanho = bloco or BLOCO_ARQUIVO
    try:
        while dados := await em_thread_lake(arq.arquivo.read, tamanho):
            yield dados
    finally:
        arq.fechar()
