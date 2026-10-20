"""Cause of a QUERY error that may be shown on screen (`conexoes.diagnostico`).

The case that motivated the module: a business guard in PL/pgSQL (`RAISE`, SQLSTATE P0001) reached
the UI as "invalid SQL or source", sending people to debug correct SQL. These tests pin both
halves of the contract -- what PASSES (server message about the query) and what does NOT pass
(anything that describes the connection).
"""

from __future__ import annotations

import duckdb
import pytest
from asyncpg import exceptions as pg

from cflow_core.conexoes.diagnostico import REDIGIDO, causa_consulta

GUARDA = "ENVIRONMENT GUARD: pipeline published for prd but the database is in stg"


def test_raise_de_plpgsql_passa():
    """The real case: an environment guard raised by the database."""
    assert causa_consulta(pg.RaiseError(GUARDA)) == GUARDA


@pytest.mark.parametrize(
    "exc",
    [
        pg.UndefinedColumnError('column "empresaa" does not exist'),  # 42703
        pg.UndefinedFunctionError("function billing.f_x() does not exist"),  # 42883
        pg.DivisionByZeroError("division by zero"),  # 22012
        pg.InsufficientPrivilegeError("permission denied for table payment_item"),  # 42501
    ],
)
def test_erro_de_consulta_do_postgres_passa(exc):
    assert causa_consulta(exc) == str(exc)


@pytest.mark.parametrize(
    "exc",
    [
        pg.InvalidPasswordError('password authentication failed for user "cflow_app"'),  # 28P01
        pg.InvalidCatalogNameError('database "hub_db" does not exist'),  # 3D000
        pg.ConnectionDoesNotExistError("connection was closed"),  # 08003
        pg.AdminShutdownError("terminating connection due to administrator command"),  # 57P01
        pg.InternalServerError("tuplestore corrupted"),  # XX000
    ],
)
def test_erro_que_descreve_a_conexao_nao_passa(exc):
    """These messages cite the user, database or the server itself -- stay generic."""
    assert causa_consulta(exc) is None


def test_erro_do_duckdb_passa_so_a_primeira_linha():
    try:
        duckdb.connect().execute("SELECT * FROM missing_table")
    except duckdb.Error as exc:
        causa = causa_consulta(exc)
    assert causa is not None
    assert "missing_table" in causa
    assert "\n" not in causa  # the rest (LINE/^) stays in the server log


def test_excecao_desconhecida_nao_vaza():
    """Allow-list: what the module does not recognize stays silent instead of leaking."""
    assert causa_consulta(ValueError("host=db.internal.example user=acme_query")) is None
    assert causa_consulta(OSError("Connect call failed ('db.internal.example', 6432)")) is None


def test_valor_de_parametro_segredo_e_redigido():
    """Postgres echoes the bound value in a cast error -- even if it is the password."""
    senha = "S3cr3t-in-clear-text"
    exc = pg.InvalidTextRepresentationError(f'invalid input syntax for type integer: "{senha}"')
    causa = causa_consulta(exc, segredos=[senha])
    assert senha not in causa
    assert REDIGIDO in causa


def test_segredo_curto_nao_redige():
    """Redacting "1"/"ok" would erase pieces of any sentence -- and it is not a secret."""
    exc = pg.UndefinedColumnError('column "ok" does not exist')
    assert causa_consulta(exc, segredos=["ok"]) == 'column "ok" does not exist'


def test_trunca_em_300():
    assert len(causa_consulta(pg.RaiseError("x" * 5000))) == 300


def test_sqlserver_raiserror_passa():
    """`RAISERROR` (50000) lands in `OperationalError` in pytds: filtering by class would silence the
    SQL Server environment guard. The discriminator is `msg_no`."""
    import pytds

    exc = pytds.OperationalError(GUARDA)
    exc.msg_no = 50000
    exc.text = GUARDA
    exc.srvname = "SRV-PRD"
    causa = causa_consulta(exc)
    assert causa == GUARDA
    assert "SRV-PRD" not in causa  # server name describes the connection


def test_sqlserver_login_e_erro_sem_msg_no_nao_passam():
    import pytds

    login = pytds.LoginError("Login failed for user 'acme'")
    login.msg_no = 18456
    assert causa_consulta(login) is None

    # Without `msg_no` the message did not come from the server: it is transport (timeout, socket).
    assert causa_consulta(pytds.OperationalError("Login timeout expired")) is None

    banco = pytds.OperationalError('Cannot open database "hub_db" requested by the login.')
    banco.msg_no = 4060
    banco.text = 'Cannot open database "hub_db" requested by the login.'
    assert causa_consulta(banco) is None
