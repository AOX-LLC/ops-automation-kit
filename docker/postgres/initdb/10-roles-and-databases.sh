#!/bin/sh
# Create the n8n and app databases with separate roles. Runs once, on an empty data dir.
# Passwords come from secret files and reach SQL only as psql variables quoted with :'var'.
set -eu

SECRETS=/run/kit-secrets

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -v n8n_password="$(cat "$SECRETS/n8n_db_password")" \
    -v owner_password="$(cat "$SECRETS/opskit_owner_password")" \
    -v app_password="$(cat "$SECRETS/opskit_app_password")" <<'SQL'
CREATE ROLE n8n_user LOGIN PASSWORD :'n8n_password';
CREATE ROLE opskit_owner LOGIN PASSWORD :'owner_password';
CREATE ROLE opskit_app LOGIN PASSWORD :'app_password';

CREATE DATABASE n8n OWNER n8n_user;
CREATE DATABASE opskit OWNER opskit_owner;

REVOKE ALL ON DATABASE n8n FROM PUBLIC;
REVOKE ALL ON DATABASE opskit FROM PUBLIC;
GRANT CONNECT ON DATABASE opskit TO opskit_app;
SQL

# Nobody but the owner may create objects in the app database's public schema.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname opskit <<'SQL'
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO opskit_owner;
SQL
