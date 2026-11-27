#!/bin/sh
# Applies database/migrations/*.sql once each, in order, then sets up the service roles.
# Runs as a one-shot container with the owner's credentials; safe to run again at any time, and
# works on a new database as well as on one that already holds data.
set -eu
export PGHOST="${PGHOST:-postgres}" PGUSER="$POSTGRES_USER" PGPASSWORD="$POSTGRES_PASSWORD" \
       PGDATABASE="${POSTGRES_DB:-simucore}"
run() { psql -v ON_ERROR_STOP=1 -q "$@"; }

run -c "CREATE TABLE IF NOT EXISTS schema_migration (
          name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"

for f in /migrations/*.sql; do
  name=$(basename "$f")
  if [ "$(run -tAc "SELECT count(*) FROM schema_migration WHERE name = '$name'")" = 0 ]; then
    echo "applying $name"
    # One transaction: the migration and the record of it succeed or fail together.
    run -1 -f "$f" -c "INSERT INTO schema_migration(name) VALUES ('$name')"
  else
    echo "already applied $name"
  fi
done

# Passwords are passed as psql variables, so they are quoted by psql and not visible in SQL text.
run -v writer="$MONITOR_DB_PASSWORD" -v exporter="$EXPORTER_DB_PASSWORD" \
    -v twin="$TWIN_DB_PASSWORD" -v ai="$AI_DB_PASSWORD" <<'SQL'
SELECT 'CREATE ROLE monitor_writer LOGIN' WHERE NOT EXISTS
  (SELECT 1 FROM pg_roles WHERE rolname = 'monitor_writer') \gexec
SELECT 'CREATE ROLE metrics_exporter LOGIN' WHERE NOT EXISTS
  (SELECT 1 FROM pg_roles WHERE rolname = 'metrics_exporter') \gexec
SELECT 'CREATE ROLE twin_writer LOGIN' WHERE NOT EXISTS
  (SELECT 1 FROM pg_roles WHERE rolname = 'twin_writer') \gexec
SELECT 'CREATE ROLE ai_writer LOGIN' WHERE NOT EXISTS
  (SELECT 1 FROM pg_roles WHERE rolname = 'ai_writer') \gexec
ALTER ROLE ai_writer PASSWORD :'ai' CONNECTION LIMIT 10;
ALTER ROLE twin_writer PASSWORD :'twin' CONNECTION LIMIT 10;
ALTER ROLE monitor_writer PASSWORD :'writer' CONNECTION LIMIT 20;
ALTER ROLE metrics_exporter PASSWORD :'exporter' CONNECTION LIMIT 5;
GRANT pg_monitor TO metrics_exporter;

-- The monitoring service appends samples and manages its own events. It cannot change or delete
-- history, alter tables, or see anything else.
GRANT USAGE ON SCHEMA public TO monitor_writer;
GRANT SELECT, INSERT ON metric_sample, infra_metric_sample, link_metric_sample TO monitor_writer;
GRANT SELECT, INSERT, UPDATE ON monitoring_event TO monitor_writer;
GRANT SELECT, INSERT ON telemetry_source TO monitor_writer;
GRANT EXECUTE ON FUNCTION ensure_metric_partitions(integer),
                          drop_old_metric_partitions(integer) TO monitor_writer;

-- The digital twin appends its own history and reads measurements. It cannot change either.
GRANT USAGE ON SCHEMA public TO twin_writer;
GRANT SELECT ON metric_sample, infra_metric_sample, link_metric_sample TO twin_writer;
GRANT SELECT, INSERT ON twin_structure, twin_transition, twin_snapshot TO twin_writer;
GRANT EXECUTE ON FUNCTION prune_twin_history(integer) TO twin_writer;

-- The AI service records its own opinions. It cannot see measurements in the database (it gets them
-- from the monitoring stream), nor change what others recorded.
GRANT USAGE ON SCHEMA public TO ai_writer;
GRANT SELECT, INSERT ON ml_model, prediction, prediction_outcome TO ai_writer;
GRANT SELECT, INSERT, UPDATE ON anomaly_event, prediction_warning TO ai_writer;
GRANT EXECUTE ON FUNCTION close_stale_ai_rows(), prune_ai_history(integer) TO ai_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_reader;
SQL
echo "migrations complete"
