-- AI module: anomaly events with a beginning and an end, predictions with their later outcome, and
-- warnings. Anomalies (unsupervised) and predictions (supervised) are kept apart on purpose.

ALTER TABLE ml_model ADD COLUMN fingerprint text UNIQUE;  -- of the model files

-- ---------------------------------------------------------------- anomaly detection
ALTER TABLE anomaly_event
  ADD COLUMN last_seen  timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN closed_at  timestamptz,
  ADD COLUMN level      text NOT NULL DEFAULT 'unusual' CHECK (level IN ('unusual', 'abnormal')),
  ADD COLUMN condition  text NOT NULL DEFAULT 'resources',
  ADD COLUMN origin     text NOT NULL DEFAULT 'measured'
    CHECK (origin IN ('measured', 'synthetic'));
ALTER TABLE anomaly_event ADD CHECK (closed_at >= ts);
CREATE INDEX anomaly_event_ts_idx ON anomaly_event (ts DESC);

-- ---------------------------------------------------------------- failure prediction
ALTER TABLE prediction
  ADD COLUMN origin text NOT NULL DEFAULT 'measured' CHECK (origin IN ('measured', 'synthetic'));
CREATE INDEX prediction_ts_idx ON prediction (predicted_at DESC);

-- A warning: what people are shown when a probability stays high.
CREATE TABLE prediction_warning (
  id             uuid PRIMARY KEY,
  node_id        text NOT NULL,
  target         text NOT NULL CHECK (target IN ('degradation', 'failure')),
  opened_at      timestamptz NOT NULL,
  closed_at      timestamptz CHECK (closed_at >= opened_at),
  horizon_s      integer NOT NULL CHECK (horizon_s > 0),
  probability    real NOT NULL CHECK (probability BETWEEN 0 AND 1),      -- when it opened
  peak           real NOT NULL CHECK (peak BETWEEN 0 AND 1),
  reasons        jsonb NOT NULL DEFAULT '[]',
  outcome        text NOT NULL DEFAULT 'open'
    CHECK (outcome IN ('open', 'came_true', 'did_not_happen', 'unresolved')),
  lead_s         real,                                  -- warning to event, if it came true
  model_id       uuid REFERENCES ml_model(id),
  origin         text NOT NULL DEFAULT 'measured' CHECK (origin IN ('measured', 'synthetic'))
);
CREATE INDEX prediction_warning_opened_idx ON prediction_warning (opened_at DESC);
CREATE INDEX prediction_warning_node_idx ON prediction_warning (node_id, opened_at DESC);

-- What a stopped service left open cannot be judged any more.
CREATE FUNCTION close_stale_ai_rows() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE n integer; total integer := 0;
BEGIN
  UPDATE anomaly_event SET closed_at = greatest(last_seen, ts) WHERE closed_at IS NULL;
  GET DIAGNOSTICS n = ROW_COUNT; total := total + n;
  UPDATE prediction_warning SET closed_at = opened_at, outcome = 'unresolved'
    WHERE outcome = 'open';
  GET DIAGNOSTICS n = ROW_COUNT; total := total + n;
  RETURN total;
END $$;

CREATE FUNCTION prune_ai_history(keep_days integer) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE n integer; total integer := 0; before timestamptz;
BEGIN
  IF keep_days < 7 THEN RAISE EXCEPTION 'refusing to keep less than 7 days'; END IF;
  before := now() - make_interval(days => keep_days);
  DELETE FROM prediction WHERE predicted_at < before;       -- outcomes go with them
  GET DIAGNOSTICS n = ROW_COUNT; total := total + n;
  DELETE FROM anomaly_event WHERE closed_at < before;
  GET DIAGNOSTICS n = ROW_COUNT; total := total + n;
  DELETE FROM prediction_warning WHERE closed_at < before;
  GET DIAGNOSTICS n = ROW_COUNT; total := total + n;
  RETURN total;
END $$;

REVOKE ALL ON FUNCTION close_stale_ai_rows() FROM PUBLIC;
REVOKE ALL ON FUNCTION prune_ai_history(integer) FROM PUBLIC;
