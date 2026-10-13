-- Disposable synthetic demo only. Passwords are psql variables from mounted files.
-- No credential appears in this file, fixtures, manifest or command arguments.
CREATE ROLE source_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE demo_reader LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'demo_reader_password';
CREATE ROLE fixture_reader LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'fixture_reader_password';
CREATE ROLE fixture_observer LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'fixture_observer_password';
GRANT pg_read_all_stats TO fixture_observer WITH INHERIT TRUE;
REVOKE ALL ON DATABASE gateway_demo FROM PUBLIC;
GRANT CONNECT ON DATABASE gateway_demo TO demo_reader, fixture_reader, fixture_observer;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA demo_source AUTHORIZATION source_owner;
CREATE SCHEMA test_source AUTHORIZATION source_owner;
REVOKE ALL ON SCHEMA demo_source, test_source FROM PUBLIC;
ALTER ROLE demo_reader SET default_transaction_read_only = on;
ALTER ROLE fixture_reader SET default_transaction_read_only = on;
ALTER ROLE fixture_observer SET default_transaction_read_only = on;
ALTER ROLE demo_reader SET statement_timeout = '25s';
ALTER ROLE fixture_reader SET statement_timeout = '25s';
ALTER ROLE fixture_observer SET statement_timeout = '5s';
ALTER ROLE demo_reader SET lock_timeout = '5s';
ALTER ROLE fixture_reader SET lock_timeout = '5s';
BEGIN;
SET LOCAL ROLE source_owner;
CREATE TABLE demo_source.orders (
    id text PRIMARY KEY, tenant_id text NOT NULL, access_group text NOT NULL,
    ordered_at timestamptz NOT NULL, segment text, amount numeric(38,2) NOT NULL
);
RESET ROLE;
COPY demo_source.orders FROM '/fixtures/orders.csv' WITH (FORMAT csv, HEADER true);
SET LOCAL ROLE source_owner;
CREATE TABLE test_source.orders (LIKE demo_source.orders INCLUDING ALL);
INSERT INTO test_source.orders SELECT * FROM demo_source.orders;
CREATE TABLE test_source.boundary_orders (LIKE demo_source.orders INCLUDING ALL);
RESET ROLE;
COPY test_source.boundary_orders FROM '/fixtures/boundary-orders.csv' WITH (FORMAT csv, HEADER true);
SET LOCAL ROLE source_owner;
CREATE TABLE demo_source.watermark (tenant_id text PRIMARY KEY, loaded_at timestamptz NOT NULL);
-- Captured once during the seed transaction, persisted across restarts. This is
-- source-load freshness, not event recency and never a query-time watermark.
INSERT INTO demo_source.watermark SELECT DISTINCT tenant_id, transaction_timestamp() FROM demo_source.orders;
CREATE TABLE test_source.watermark (LIKE demo_source.watermark INCLUDING ALL);
INSERT INTO test_source.watermark SELECT * FROM demo_source.watermark;
CREATE FUNCTION test_source.bounded_delay(seconds integer) RETURNS boolean
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog AS $$
BEGIN
    IF seconds < 1 OR seconds > 90 THEN RAISE EXCEPTION 'Invalid synthetic delay'; END IF;
    PERFORM pg_sleep(seconds);
    RETURN true;
END;
$$;
REVOKE ALL ON FUNCTION test_source.bounded_delay(integer) FROM PUBLIC;
CREATE VIEW test_source.slow_orders AS SELECT o.* FROM test_source.orders o
CROSS JOIN (SELECT test_source.bounded_delay(12) AS done OFFSET 0) delay WHERE delay.done;
CREATE VIEW test_source.timeout_orders AS SELECT o.* FROM test_source.orders o
CROSS JOIN (SELECT test_source.bounded_delay(90) AS done OFFSET 0) delay WHERE delay.done;
GRANT USAGE ON SCHEMA demo_source TO demo_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA demo_source TO demo_reader;
GRANT USAGE ON SCHEMA test_source TO fixture_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA test_source TO fixture_reader;
GRANT EXECUTE ON FUNCTION test_source.bounded_delay(integer) TO fixture_reader;
COMMIT;
