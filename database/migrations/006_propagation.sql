-- Failure propagation events of the digital twin: what a failure was expected to affect, and,
-- a minute later, what it did affect according to the measurements.

CREATE TABLE propagation_event (
  id             uuid PRIMARY KEY,
  opened_at      timestamptz NOT NULL,
  run_id         uuid NOT NULL,          -- the twin process
  origins        text[] NOT NULL,        -- confirmed failures that nothing else explains
  new_origins    text[] NOT NULL,        -- those that opened this event
  graph          text NOT NULL,          -- checksum of the dependency graph used
  level          text NOT NULL CHECK (level IN ('none', 'low', 'moderate', 'high', 'severe')),
  affected       integer NOT NULL CHECK (affected >= 0),        -- at least moderate
  expected       real NOT NULL CHECK (expected >= 0),           -- sum of the impacts
  business_impact real NOT NULL CHECK (business_impact >= 0),
  predicted      text[] NOT NULL,
  report         jsonb NOT NULL,
  closed_at      timestamptz CHECK (closed_at >= opened_at),
  outcome        jsonb,                  -- what really showed trouble, once the minute is over
  origin         text CHECK (origin IN ('measured', 'synthetic'))
);
CREATE INDEX propagation_event_opened_idx ON propagation_event (opened_at DESC);
CREATE INDEX propagation_event_origins_idx ON propagation_event USING gin (origins);

CREATE OR REPLACE FUNCTION prune_twin_history(keep_days integer) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE n integer; total integer := 0;
BEGIN
  IF keep_days < 7 THEN RAISE EXCEPTION 'refusing to keep less than 7 days'; END IF;
  DELETE FROM twin_transition WHERE ts < now() - make_interval(days => keep_days);
  GET DIAGNOSTICS n = ROW_COUNT; total := total + n;
  DELETE FROM twin_snapshot WHERE ts < now() - make_interval(days => keep_days);
  GET DIAGNOSTICS n = ROW_COUNT; total := total + n;
  DELETE FROM propagation_event WHERE opened_at < now() - make_interval(days => keep_days);
  GET DIAGNOSTICS n = ROW_COUNT; total := total + n;
  RETURN total;
END $$;
REVOKE ALL ON FUNCTION prune_twin_history(integer) FROM PUBLIC;
