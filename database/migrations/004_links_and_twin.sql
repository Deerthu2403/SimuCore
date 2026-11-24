-- Network connections, the bank's database pool, and the history of the digital twin.

ALTER TABLE metric_sample
  ADD COLUMN db_pool_in_use  real,
  ADD COLUMN db_pool_size    real,
  ADD COLUMN db_failure_rate real;

-- ---------------------------------------------------------------- network connections
CREATE TABLE link_metric_sample (
  ts           timestamptz NOT NULL,
  source       text NOT NULL,
  target       text NOT NULL,
  calls_per_s  real,
  failure_rate real,
  latency_ms   real,
  circuit      real,
  origin       text NOT NULL DEFAULT 'measured' CHECK (origin IN ('measured', 'synthetic')),
  PRIMARY KEY (source, target, ts, origin)
) PARTITION BY RANGE (ts);
CREATE TABLE link_metric_sample_default PARTITION OF link_metric_sample DEFAULT;

-- ---------------------------------------------------------------- digital twin history
-- What the twin believed, as opposed to what was measured. Statuses only: metrics at a point in
-- time are in the *_metric_sample tables, so they are stored once.
CREATE TABLE twin_structure (
  checksum    text PRIMARY KEY,        -- of the structure itself, so equal structures are one row
  created_at  timestamptz NOT NULL DEFAULT now(),
  components  jsonb NOT NULL,
  links       jsonb NOT NULL
);

CREATE TABLE twin_transition (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts           timestamptz NOT NULL,
  run_id       uuid NOT NULL,          -- one run of the twin process
  version      bigint NOT NULL,        -- twin version that contained the change
  object_kind  text NOT NULL CHECK (object_kind IN ('component', 'link')),
  object_id    text NOT NULL,
  from_status  text,
  to_status    text NOT NULL,
  reason       text NOT NULL DEFAULT '',
  root_causes  text[] NOT NULL DEFAULT '{}',
  origin       text CHECK (origin IN ('measured', 'synthetic')),
  UNIQUE (run_id, version, object_id)
);
CREATE INDEX twin_transition_object_idx ON twin_transition (object_id, ts DESC);
CREATE INDEX twin_transition_ts_idx ON twin_transition (ts DESC);

CREATE TABLE twin_snapshot (
  ts                timestamptz NOT NULL,
  run_id            uuid NOT NULL,
  version           bigint NOT NULL,
  structure         text NOT NULL REFERENCES twin_structure(checksum),
  origin            text CHECK (origin IN ('measured', 'synthetic')),
  statuses          jsonb NOT NULL,    -- {object id: status} for every component and link
  PRIMARY KEY (run_id, ts)            -- also while nothing changes: proof that it was running
);
CREATE INDEX twin_snapshot_ts_idx ON twin_snapshot (ts DESC);

CREATE FUNCTION prune_twin_history(keep_days integer) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE n integer; total integer := 0;
BEGIN
  IF keep_days < 7 THEN RAISE EXCEPTION 'refusing to keep less than 7 days'; END IF;
  DELETE FROM twin_transition WHERE ts < now() - make_interval(days => keep_days);
  GET DIAGNOSTICS n = ROW_COUNT; total := total + n;
  DELETE FROM twin_snapshot WHERE ts < now() - make_interval(days => keep_days);
  GET DIAGNOSTICS n = ROW_COUNT; total := total + n;
  RETURN total;
END $$;
REVOKE ALL ON FUNCTION prune_twin_history(integer) FROM PUBLIC;

-- ---------------------------------------------------------------- partitions: one more table
CREATE OR REPLACE FUNCTION ensure_metric_partitions(months_ahead integer DEFAULT 2)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  parent text; part text; m date; created integer := 0;
BEGIN
  IF months_ahead NOT BETWEEN 0 AND 24 THEN RAISE EXCEPTION 'months_ahead out of range'; END IF;
  FOREACH parent IN ARRAY ARRAY['metric_sample', 'infra_metric_sample', 'link_metric_sample'] LOOP
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

CREATE OR REPLACE FUNCTION drop_old_metric_partitions(keep_days integer) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  r record; ends date; dropped integer := 0;
BEGIN
  IF keep_days < 7 THEN RAISE EXCEPTION 'refusing to keep less than 7 days'; END IF;
  FOR r IN SELECT c.relname FROM pg_inherits i
           JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent
           WHERE p.relname IN ('metric_sample', 'infra_metric_sample', 'link_metric_sample')
             AND c.relname ~ '_y[0-9]{4}m[0-9]{2}$' LOOP
    ends := (to_date(right(r.relname, 7), 'YYYY"m"MM') + interval '1 month')::date;
    IF ends < (now() AT TIME ZONE 'UTC')::date - keep_days THEN
      EXECUTE format('DROP TABLE %I', r.relname);
      dropped := dropped + 1;
    END IF;
  END LOOP;
  RETURN dropped;
END $$;
