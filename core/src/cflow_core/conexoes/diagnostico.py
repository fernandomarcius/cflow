"""VISIBLE cause of a QUERY error on the source.

`conectores/*._causa` covers CONNECTION failures with a closed vocabulary ("authentication refused",
"host unreachable", "database does not exist"): there the driver message describes the connection
(host, user, sometimes the database) and none of it leaves the platform.

A QUERY error is different. The text comes from the SERVER, is about the SQL the specialist wrote,
and is exactly what tells them what to do, including when it is a business-guard `RAISE`. For
example, a node running `select f_assert_environment('prd')` may raise
`ENVIRONMENT GUARD: pipeline published for prd but the host is hml`; showing a generic "invalid SQL
or source" would send the user to debug SQL that was correct, without saying that the guard fired
and why.

**ALLOW-LIST by error class, never a deny-list.** Only what the server raised about the query
passes; anything this module does not recognise returns `None` and the caller stays generic. A new
driver error therefore starts out silent rather than leaking.

Applies to the SQL engines (DuckDB, Postgres, SQL Server), where the server message IS the
diagnosis. `arquivo`/`api_rest`/`api_query` sources are left out on purpose: there the driver
message carries file paths, URLs and headers, which describe the connection, not the query.
"""

from __future__ import annotations

from collections.abc import Iterable

REDIGIDO = "«redacted»"
_MAX = 300

# SQLSTATE classes (first 2 characters) where Postgres speaks about the QUERY. Outside these the
# message describes the connection or the server and stays generic: 08 (connection exception),
# 28 (invalid authorization, names the user), 3D (invalid catalog name, names the database),
# 53 (insufficient resources), 57 (operator intervention), XX (internal error).
_PG_CONSULTA = frozenset(
    {
        "0A",  # feature not supported
        "22",  # data exception (invalid cast, division by zero, out-of-range value)
        "23",  # integrity constraint violation
        "2F",  # error inside an SQL function
        "38",  # external routine exception
        "39",  # external routine invocation exception
        "42",  # syntax error, missing column/table/function, permission denied on the object
        "54",  # program limit exceeded
        "P0",  # PL/pgSQL: user RAISE (P0001); business guards land here
    }
)


# SQL Server message numbers that describe the LOGIN, not the command (the same list pytds uses to
# decide whether a connection retry is worthwhile). Their message names the user and the database.
_MSSQL_LOGIN = frozenset(
    {
        18456,  # login failed for user
        18486,  # account is locked
        18487,  # password expired
        18488,  # password should be changed
        18452,  # login from untrusted domain
        4060,  # cannot open database requested by the login
    }
)


def _bruto(exc: BaseException) -> str | None:
    """Raw server message, when the error is known to be about the query."""
    try:
        import duckdb
    except ModuleNotFoundError:  # pragma: no cover — duckdb is a core dependency
        duckdb = None  # type: ignore[assignment]
    if duckdb is not None and isinstance(exc, duckdb.Error):
        # DuckDB runs in-process with no connection to describe: every error is about the query.
        return str(exc)

    try:
        import asyncpg
    except ModuleNotFoundError:  # pragma: no cover
        asyncpg = None  # type: ignore[assignment]
    if asyncpg is not None and isinstance(exc, asyncpg.PostgresError):
        sqlstate = getattr(exc, "sqlstate", None) or ""
        if sqlstate[:2] not in _PG_CONSULTA:
            return None
        # `.message` is the server M field; asyncpg `str(exc)` may carry extra context.
        return getattr(exc, "message", None) or str(exc)

    try:
        import pytds
    except ModuleNotFoundError:  # pragma: no cover
        pytds = None  # type: ignore[assignment]
    if pytds is not None and isinstance(exc, pytds.Error):
        # Here the class does NOT discriminate: pytds decides by message number and only 5 numbers
        # become `ProgrammingError` (syntax, missing column/object, ...). `RAISERROR`/`THROW` (50000),
        # division by zero (8134) and almost every T-SQL error fall into `OperationalError`, which is
        # also where connection failures live. Filtering by class would silence exactly the
        # SQL Server equivalent of a business guard.
        # The honest discriminator is `msg_no`: pytds initialises it to ZERO and only fills it when
        # the message came from the SERVER (`tds_base._create_exception_by_message`), so `0`/missing
        # is a transport error (timeout, socket), which describes the connection. Login failures also
        # come from the server, so their numbers are excluded: the message names the user and database.
        msg_no = getattr(exc, "msg_no", 0)
        if isinstance(exc, pytds.LoginError) or not msg_no or msg_no in _MSSQL_LOGIN:
            return None
        # `.text` is that error's message. `str(exc)` may carry the session's other messages
        # concatenated, and `.srvname` (the server name) is deliberately left out.
        return getattr(exc, "text", None) or str(exc)

    return None


def _redigir(texto: str, segredos: Iterable[str]) -> str:
    """Erases the value of `segredo: true` parameters from the text.

    The server echoes bound values in some messages ("invalid input syntax for type integer:
    \"abc\""). If that value is the password the specialist passed as a parameter, the message this
    module releases would carry it to the screen, and the front end displays it. Fewer than 4
    characters is not a secret, and redacting "1" or "ok" would erase pieces of any sentence."""
    for s in segredos:
        if isinstance(s, str) and len(s) >= 4 and s in texto:
            texto = texto.replace(s, REDIGIDO)
    return texto


def causa_consulta(exc: BaseException, *, segredos: Iterable[str] = ()) -> str | None:
    """First line of the server message, sanitised and truncated, or `None` (stay generic).

    First line because that is where the engine puts the cause; the rest (`LINE 3: ^`, PL/pgSQL
    context, driver stack) stays in the server log, where it is already recorded."""
    bruto = _bruto(exc)
    if not bruto:
        return None
    causa = _redigir(bruto.strip().splitlines()[0].strip(), segredos)
    return causa[:_MAX] or None
