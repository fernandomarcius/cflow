"""Alembic environment (async). Connects as cflow_owner (the schema owner).

The URL comes from CFLOW_OWNER_DATABASE_URL. The migrations are explicit SQL (no autogenerate),
so there is no target_metadata. Secrets delivered as files (`CFLOW_APP_PASSWORD`,
`CFLOW_ADMIN_PASSWORD`) become variables before any migration reads them
(see `cflow_core.runtime`).
"""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from cflow_core.runtime import contexto_ssl, exportar_segredos_arquivo

config = context.config

target_metadata = None


exportar_segredos_arquivo()


def _owner_url() -> str:
    url = os.environ.get("CFLOW_OWNER_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "CFLOW_OWNER_DATABASE_URL is not set - Alembic must connect as cflow_owner."
        )
    return url


def _connect_args() -> dict:
    args: dict = {}
    if os.environ.get("CFLOW_DATABASE_TLS", "").lower() in ("1", "true", "yes"):
        args["ssl"] = contexto_ssl(os.environ.get("CFLOW_DATABASE_CA_FILE"))
    schema = os.environ.get("CFLOW_DATABASE_SCHEMA")
    if schema:
        args["server_settings"] = {"search_path": schema}
    return args


def run_migrations_offline() -> None:
    context.configure(
        url=_owner_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_owner_url(), future=True, connect_args=_connect_args())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
