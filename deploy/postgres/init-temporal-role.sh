#!/bin/sh
# Runs once, when the Postgres data directory is first initialised (docker-entrypoint-initdb.d).
# Creates the dedicated role of Temporal: it creates and migrates its own schema with
# `temporal-sql-tool`, while ours is migrated by Alembic - two owners for the same role is how
# responsibility gets lost. The databases are created by the `temporal-setup` service, hence CREATEDB.
set -eu
: "${TEMPORAL_DB_USER:=temporal_app}"
: "${TEMPORAL_DB_PASSWORD:=change-me-temporal}"
case "$TEMPORAL_DB_USER" in
  *[!A-Za-z0-9_]*) echo "invalid TEMPORAL_DB_USER: use letters, digits and _ only" >&2; exit 1 ;;
esac
psql -v ON_ERROR_STOP=1 -v pwd="$TEMPORAL_DB_PASSWORD" --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE ${TEMPORAL_DB_USER} LOGIN CREATEDB PASSWORD :'pwd';
SQL
