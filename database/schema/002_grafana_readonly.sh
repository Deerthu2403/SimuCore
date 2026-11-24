#!/bin/sh
# Grafana only ever reads, so it gets a role that can do nothing else.
set -eu
psql -v ON_ERROR_STOP=1 -v pw="$GRAFANA_DB_PASSWORD" -v owner="$POSTGRES_USER" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE grafana_reader LOGIN PASSWORD :'pw';
ALTER ROLE grafana_reader SET default_transaction_read_only = on;
GRANT USAGE ON SCHEMA public TO grafana_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public GRANT SELECT ON TABLES TO grafana_reader;
SQL
