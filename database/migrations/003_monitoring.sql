-- Monitoring module: full metric set, origin of every sample, infrastructure samples, monitoring
-- events, and partition maintenance that an unprivileged role may call.

-- ---------------------------------------------------------------- service metrics
ALTER TABLE metric_sample
  ADD COLUMN mem_bytes          double precision,
  ADD COLUMN latency_mean_ms    real,
  ADD COLUMN net_rx_bps         double precision,
  ADD COLUMN net_tx_bps         double precision,
  ADD COLUMN db_response_ms     real,
  ADD COLUMN active_connections real,
  ADD COLUMN queue_length       real,
  ADD COLUMN in_flight          real,
  ADD COLUMN status             text NOT NULL DEFAULT 'up'
    CHECK (status IN ('up', 'down', 'unresponsive')),
  ADD COLUMN origin             text NOT NULL DEFAULT 'measured'
    CHECK (origin IN ('measured', 'synthetic'));

-- Synthetic data may cover the same period as measured data without ever replacing it.
ALTER TABLE metric_sample DROP CONSTRAINT metric_sample_pkey;
ALTER TABLE metric_sample ADD PRIMARY KEY (node_id, ts, origin);

-- ---------------------------------------------------------------- infrastructure metrics
CREATE TABLE infra_metric_sample (
  ts         timestamptz NOT NULL,
  component  text NOT NULL,
  status     text NOT NULL CHECK (status IN ('up', 'down', 'unresponsive')),
  metrics    jsonb NOT NULL DEFAULT '{}',
  origin     text NOT NULL DEFAULT 'measured' CHECK (origin IN ('measured', 'synthetic')),
  PRIMARY KEY (component, ts, origin)
) PARTITION BY RANGE (ts);
CREATE TABLE infra_metric_sample_default PARTITION OF infra_metric_sample DEFAULT;

-- ---------------------------------------------------------------- monitoring events
CREATE TABLE monitoring_event (
  id         uuid PRIMARY KEY,
  opened_at  timestamptz NOT NULL,
  last_seen  timestamptz NOT NULL,
  closed_at  timestamptz CHECK (closed_at >= opened_at),
  kind       text NOT NULL,
  severity   severity NOT NULL,
  service    text,
  metric     text,
  message    text NOT NULL
);
CREATE INDEX monitoring_event_time_idx ON monitoring_event (opened_at DESC);
CREATE UNIQUE INDEX monitoring_event_one_open_idx ON monitoring_event
  (kind, coalesce(service, ''), coalesce(metric, '')) WHERE closed_at IS NULL;

-- ---------------------------------------------------------------- partitions
-- Creates the monthly partitions from this month to ``months_ahead`` months from now. Rows that
-- reached the default partition first are moved, so being late is repaired instead of refused.
CREATE FUNCTION ensure_metric_partitions(months_ahead integer DEFAULT 2) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  parent text; part text; m date; created integer := 0;
BEGIN
  IF months_ahead NOT BETWEEN 0 AND 24 THEN RAISE EXCEPTION 'months_ahead out of range'; END IF;
  FOREACH parent IN ARRAY ARRAY['metric_sample', 'infra_metric_sample'] LOOP
    FOR i IN 0..months_ahead LOOP
      m := (date_trunc('month', now() AT TIME ZONE 'UTC') + make_interval(months => i))::date;
      part := format('%s_y%sm%s', parent, to_char(m, 'YYYY'), to_char(m, 'MM'));
      IF to_regclass(part) IS NULL THEN
        EXECUTE format('CREATE TABLE %I (LIKE %I INCLUDING DEFAULTS INCLUDING CONSTRAINTS)',
                       part, parent);
        EXECUTE format(
          'WITH moved AS (DELETE FROM %I WHERE ts >= %L AND ts < %L RETURNING *) '
          'INSERT INTO %I SELECT * FROM moved',
          parent || '_default', m::timestamp AT TIME ZONE 'UTC',
          (m + interval '1 month')::timestamp AT TIME ZONE 'UTC', part);
        EXECUTE format('ALTER TABLE %I ATTACH PARTITION %I FOR VALUES FROM (%L) TO (%L)',
          parent, part, m::timestamp AT TIME ZONE 'UTC',
          (m + interval '1 month')::timestamp AT TIME ZONE 'UTC');
        EXECUTE format('GRANT SELECT ON %I TO grafana_reader', part);
        created := created + 1;
      END IF;
    END LOOP;
  END LOOP;
  RETURN created;
END $$;

-- Drops monthly partitions that ended more than ``keep_days`` days ago. Never the default one.
CREATE FUNCTION drop_old_metric_partitions(keep_days integer) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  r record; ends date; dropped integer := 0;
BEGIN
  IF keep_days < 7 THEN RAISE EXCEPTION 'refusing to keep less than 7 days'; END IF;
  FOR r IN SELECT c.relname FROM pg_inherits i
           JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent
           WHERE p.relname IN ('metric_sample', 'infra_metric_sample')
             AND c.relname ~ '_y[0-9]{4}m[0-9]{2}$' LOOP
    ends := (to_date(right(r.relname, 7), 'YYYY"m"MM') + interval '1 month')::date;
    IF ends < (now() AT TIME ZONE 'UTC')::date - keep_days THEN
      EXECUTE format('DROP TABLE %I', r.relname);
      dropped := dropped + 1;
    END IF;
  END LOOP;
  RETURN dropped;
END $$;

REVOKE ALL ON FUNCTION ensure_metric_partitions(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION drop_old_metric_partitions(integer) FROM PUBLIC;
