#!/bin/bash
set -euo pipefail
# The official entrypoint executes this only on a new run-specific volume.
# Never print psql variables or permit xtrace here.
case "$GATEWAY_DB_KIND" in
  registry)
    owner_password=$(cat /run/secrets/registry_owner)
    app_password=$(cat /run/secrets/registry_app)
    retention_password=$(cat /run/secrets/registry_audit_retention)
    source_password=$(cat /run/secrets/registry_source_reader)
    psql --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 <<SQL
CREATE ROLE gateway_owner LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD '$owner_password';
CREATE ROLE gateway_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD '$app_password';
CREATE ROLE gateway_audit_retention LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD '$retention_password';
CREATE ROLE gateway_source_reader LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD '$source_password';
ALTER DATABASE gateway_registry OWNER TO gateway_owner;
REVOKE ALL ON DATABASE gateway_registry FROM PUBLIC;
GRANT CONNECT ON DATABASE gateway_registry TO gateway_owner, gateway_app, gateway_audit_retention;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER ROLE gateway_app SET statement_timeout='4s';
ALTER ROLE gateway_app SET lock_timeout='2s';
SQL
    ;;
  demo)
    # Fixed filenames + random base64url-only passwords. Credentials never
    # appear in process argv; stdin becomes private session psql variables.
    psql --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 <<SQL
\set demo_reader_password '$(cat /run/secrets/demo_reader)'
\set fixture_reader_password '$(cat /run/secrets/fixture_reader)'
\set fixture_observer_password '$(cat /run/secrets/fixture_observer)'
\i /opt/gateway/init.sql
SQL
    ;;
  *) printf 'Invalid synthetic database kind\n' >&2; exit 1 ;;
esac
