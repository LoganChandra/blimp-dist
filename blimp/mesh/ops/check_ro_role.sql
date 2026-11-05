-- Dedicated read-only role for sql_readonly acceptance checks (phase 2 security review).
-- Security posture change: the Boss runs this himself (rail #3), then sets
--   BLIMP_CHECK_SQL_DSN=postgresql://blimp_check_ro@/duke_data?port=5433
-- for the sweep and the brain MCP. Never grant pg_read_server_files / pg_execute_server_program.
CREATE ROLE blimp_check_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
GRANT CONNECT ON DATABASE duke_data TO blimp_check_ro;
GRANT USAGE ON SCHEMA mesh TO blimp_check_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA mesh TO blimp_check_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA mesh GRANT SELECT ON TABLES TO blimp_check_ro;
ALTER ROLE blimp_check_ro SET default_transaction_read_only = on;
ALTER ROLE blimp_check_ro SET statement_timeout = '30s';
