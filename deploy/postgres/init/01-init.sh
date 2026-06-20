#!/bin/bash
# Runs once on first cluster init (docker-entrypoint-initdb.d).
# Enables pgvector on the app DB and creates a separate Zitadel database.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-'EOSQL'
	CREATE EXTENSION IF NOT EXISTS vector;
EOSQL

if ! psql -tAc "SELECT 1 FROM pg_database WHERE datname = '${ZITADEL_DB}'" \
	--username "$POSTGRES_USER" --dbname "$POSTGRES_DB" | grep -q 1; then
	createdb --username "$POSTGRES_USER" "$ZITADEL_DB"
	echo "created database ${ZITADEL_DB}"
fi
