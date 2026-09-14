#!/bin/sh
set -eu
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -v ON_ERROR_STOP=1 <<'SQL'
\getenv app_password APP_DB_PASSWORD
\getenv migrator_password MIGRATOR_DB_PASSWORD
SELECT format('CREATE ROLE shop_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'app_password') \gexec
SELECT format('CREATE ROLE shop_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'migrator_password') \gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA shop AUTHORIZATION shop_migrator;
GRANT USAGE ON SCHEMA shop TO shop_app;
ALTER ROLE shop_app SET search_path = shop;
ALTER ROLE shop_migrator SET search_path = shop;
ALTER DEFAULT PRIVILEGES FOR ROLE shop_migrator IN SCHEMA shop GRANT SELECT, INSERT, UPDATE ON TABLES TO shop_app;
ALTER DEFAULT PRIVILEGES FOR ROLE shop_migrator IN SCHEMA shop GRANT USAGE, SELECT ON SEQUENCES TO shop_app;
SQL
