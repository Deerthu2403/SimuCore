-- SimuCore — initial PostgreSQL schema (PostgreSQL 16+)
-- Applied automatically by the postgres container (docker-entrypoint-initdb.d).
-- Convention: infra_edge.source_id DEPENDS ON infra_edge.target_id.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------- enums
CREATE TYPE node_kind AS ENUM (
  'gateway', 'service', 'database', 'cache', 'queue',
  'server', 'load_balancer', 'firewall', 'switch', 'external'
);
CREATE TYPE node_layer AS ENUM ('edge', 'application', 'data', 'compute', 'network');
CREATE TYPE edge_kind AS ENUM ('calls', 'reads_writes', 'publishes', 'hosted_on', 'routes_through');
CREATE TYPE health_status AS ENUM ('healthy', 'degraded', 'critical', 'down', 'unknown');
CREATE TYPE run_status AS ENUM ('queued', 'running', 'completed', 'failed', 'cancelled');
CREATE TYPE severity AS ENUM ('info', 'low', 'medium', 'high', 'critical');
CREATE TYPE rec_status AS ENUM ('open', 'acknowledged', 'accepted', 'rejected', 'expired');
CREATE TYPE user_role AS ENUM ('viewer', 'operator', 'engineer', 'admin');

-- ---------------------------------------------------------------- governance
CREATE TABLE app_user (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  username      text UNIQUE NOT NULL,
  display_name  text,
  role          user_role NOT NULL DEFAULT 'viewer',
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
  id          bigserial PRIMARY KEY,
  ts          timestamptz NOT NULL DEFAULT now(),
  actor       text NOT NULL,              -- username or adapter name
  action      text NOT NULL,              -- e.g. 'adapter.query', 'simulation.start'
  target      text,
  details     jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_log_ts_idx ON audit_log (ts DESC);

-- ---------------------------------------------------------------- topology
CREATE TABLE topology_version (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  version      integer UNIQUE NOT NULL,
  source       text NOT NULL,             -- adapter that produced it
  checksum     text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE infra_node (
  id                 text NOT NULL,       -- stable business id, e.g. 'payments-service'
  topology_version   integer NOT NULL REFERENCES topology_version(version) ON DELETE CASCADE,
  name               text NOT NULL,
  kind               node_kind NOT NULL,
  layer              node_layer NOT NULL,
  criticality        smallint NOT NULL CHECK (criticality BETWEEN 1 AND 5),
  capacity           jsonb NOT NULL DEFAULT '{}',   -- replicas, cpu_cores, mem_gb, max_rps
  attributes         jsonb NOT NULL DEFAULT '{}',
  PRIMARY KEY (topology_version, id)
);
CREATE INDEX infra_node_kind_idx ON infra_node (kind);
CREATE INDEX infra_node_attr_gin ON infra_node USING gin (attributes);

CREATE TABLE infra_edge (
  topology_version   integer NOT NULL,
  source_id          text NOT NULL,
  target_id          text NOT NULL,
  kind               edge_kind NOT NULL,
  weight             real NOT NULL CHECK (weight >= 0 AND weight <= 1),
  attributes         jsonb NOT NULL DEFAULT '{}',
  PRIMARY KEY (topology_version, source_id, target_id, kind),
  FOREIGN KEY (topology_version, source_id) REFERENCES infra_node(topology_version, id) ON DELETE CASCADE,
  FOREIGN KEY (topology_version, target_id) REFERENCES infra_node(topology_version, id) ON DELETE CASCADE
);
CREATE INDEX infra_edge_target_idx ON infra_edge (topology_version, target_id);

-- ---------------------------------------------------------------- telemetry
CREATE TABLE telemetry_source (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text UNIQUE NOT NULL,
  adapter     text NOT NULL,              -- 'simulated', 'prometheus', ...
  endpoint    text,
  read_only   boolean NOT NULL DEFAULT true CHECK (read_only),  -- write adapters are forbidden
  enabled     boolean NOT NULL DEFAULT true,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE metric_sample (
  ts            timestamptz NOT NULL,
  node_id       text NOT NULL,
  source_id     uuid REFERENCES telemetry_source(id),
  cpu_pct       real,
  mem_pct       real,
  latency_ms    real,
  error_rate    real,
  rps           real,
  availability  real,
  extra         jsonb NOT NULL DEFAULT '{}',
  PRIMARY KEY (node_id, ts)
) PARTITION BY RANGE (ts);
-- Partitions are created ahead of time by the backend maintenance task (Phase 2).
CREATE TABLE metric_sample_default PARTITION OF metric_sample DEFAULT;
CREATE INDEX metric_sample_ts_idx ON metric_sample (ts DESC);

CREATE TABLE node_state_snapshot (
  ts          timestamptz NOT NULL,
  twin_version bigint NOT NULL,
  node_id     text NOT NULL,
  status      health_status NOT NULL,
  metrics     jsonb NOT NULL,
  PRIMARY KEY (node_id, ts)
);

-- ---------------------------------------------------------------- intelligence
CREATE TABLE ml_model (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text NOT NULL,            -- 'anomaly-isoforest-service', 'failure-xgb'
  version       integer NOT NULL,
  algorithm     text NOT NULL,
  params        jsonb NOT NULL DEFAULT '{}',
  metrics       jsonb NOT NULL DEFAULT '{}',   -- auc, brier, f1 ...
  artefact_uri  text,
  is_active     boolean NOT NULL DEFAULT false,
  trained_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (name, version)
);

CREATE TABLE anomaly_event (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ts                   timestamptz NOT NULL DEFAULT now(),
  node_id              text NOT NULL,
  score                real NOT NULL CHECK (score BETWEEN 0 AND 1),
  severity             severity NOT NULL,
  contributing_metrics jsonb NOT NULL DEFAULT '{}',
  model_id             uuid REFERENCES ml_model(id)
);
CREATE INDEX anomaly_event_node_ts_idx ON anomaly_event (node_id, ts DESC);

CREATE TABLE prediction (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  predicted_at   timestamptz NOT NULL DEFAULT now(),
  node_id        text NOT NULL,
  target         text NOT NULL DEFAULT 'failure',   -- 'failure', 'latency_ms', ...
  horizon_s      integer NOT NULL,
  probability    real CHECK (probability BETWEEN 0 AND 1),
  predicted_value real,
  top_features   jsonb NOT NULL DEFAULT '[]',
  model_id       uuid REFERENCES ml_model(id)
);
CREATE INDEX prediction_node_ts_idx ON prediction (node_id, predicted_at DESC);

CREATE TABLE prediction_outcome (
  prediction_id  uuid PRIMARY KEY REFERENCES prediction(id) ON DELETE CASCADE,
  resolved_at    timestamptz NOT NULL DEFAULT now(),
  actual_event   boolean,                 -- did the failure occur within horizon?
  actual_value   real,
  error          real                     -- predicted - actual (regression) / brier term
);

CREATE TABLE calibration_run (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  started_at     timestamptz NOT NULL DEFAULT now(),
  target         text NOT NULL,           -- 'failure-xgb', 'simpy.service_time', ...
  model_id       uuid REFERENCES ml_model(id),
  metric_name    text NOT NULL,
  metric_before  real,
  metric_after   real,
  drift          jsonb NOT NULL DEFAULT '{}',
  action         text NOT NULL,           -- 'none', 'recalibrate', 'retrain', 'refit_params'
  status         run_status NOT NULL DEFAULT 'completed'
);

-- ---------------------------------------------------------------- simulation
CREATE TABLE simulation_scenario (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name         text NOT NULL,
  description  text,
  faults       jsonb NOT NULL DEFAULT '[]',   -- [{node_id, type, start_s, duration_s, magnitude}]
  load_multiplier real NOT NULL DEFAULT 1.0,
  duration_s   integer NOT NULL DEFAULT 600,
  created_by   uuid REFERENCES app_user(id),
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE simulation_run (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_id       uuid NOT NULL REFERENCES simulation_scenario(id) ON DELETE CASCADE,
  topology_version  integer REFERENCES topology_version(version),
  seed              integer NOT NULL,
  params_version    integer,              -- calibrated parameter set used
  status            run_status NOT NULL DEFAULT 'queued',
  kpis              jsonb NOT NULL DEFAULT '{}',
  started_at        timestamptz,
  finished_at       timestamptz
);

CREATE TABLE simulation_metric (
  run_id     uuid NOT NULL REFERENCES simulation_run(id) ON DELETE CASCADE,
  sim_t      real NOT NULL,               -- simulated seconds
  node_id    text NOT NULL,
  metrics    jsonb NOT NULL,
  PRIMARY KEY (run_id, node_id, sim_t)
);

CREATE TABLE propagation_analysis (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at     timestamptz NOT NULL DEFAULT now(),
  origin_nodes   text[] NOT NULL,
  method         text NOT NULL,           -- 'weighted_bfs', 'monte_carlo'
  impacted       jsonb NOT NULL,          -- [{node_id, impact, depth, path}]
  business_impact real
);

-- ---------------------------------------------------------------- optimisation
CREATE TABLE optimization_run (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at      timestamptz NOT NULL DEFAULT now(),
  algorithm       text NOT NULL DEFAULT 'nsga2',
  objectives      text[] NOT NULL,        -- {'cost','p95_latency','risk'}
  constraints     jsonb NOT NULL DEFAULT '{}',
  population      integer NOT NULL,
  generations     integer NOT NULL,
  seed            integer,
  status          run_status NOT NULL DEFAULT 'queued',
  finished_at     timestamptz
);

CREATE TABLE optimization_solution (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id         uuid NOT NULL REFERENCES optimization_run(id) ON DELETE CASCADE,
  rank           integer NOT NULL,        -- Pareto front rank (0 = non-dominated)
  decision       jsonb NOT NULL,          -- {service_id: {replicas, cpu, mem}}
  objective_values jsonb NOT NULL,        -- {cost: .., p95_latency: .., risk: ..}
  is_recommended boolean NOT NULL DEFAULT false
);

-- ---------------------------------------------------------------- decisions
CREATE TABLE incident (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  opened_at    timestamptz NOT NULL DEFAULT now(),
  closed_at    timestamptz,
  is_simulated boolean NOT NULL DEFAULT true,
  severity     severity NOT NULL,
  root_nodes   text[] NOT NULL,
  summary      text
);

CREATE TABLE recommendation (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at    timestamptz NOT NULL DEFAULT now(),
  incident_id   uuid REFERENCES incident(id),
  source_module text NOT NULL,            -- 'prediction', 'propagation', 'optimization', 'simulation'
  source_ref    uuid,                     -- id of originating artefact
  title         text NOT NULL,
  action        jsonb NOT NULL,           -- structured, advisory-only action description
  target_nodes  text[] NOT NULL DEFAULT '{}',
  priority      real NOT NULL,            -- probability x business impact
  confidence    real CHECK (confidence BETWEEN 0 AND 1),
  status        rec_status NOT NULL DEFAULT 'open'
);
CREATE INDEX recommendation_open_idx ON recommendation (status, priority DESC);

CREATE TABLE explanation (
  recommendation_id uuid PRIMARY KEY REFERENCES recommendation(id) ON DELETE CASCADE,
  summary     text NOT NULL,
  factors     jsonb NOT NULL DEFAULT '[]',  -- [{name, value, contribution}]
  evidence    jsonb NOT NULL DEFAULT '[]',  -- propagation paths, metric excerpts, pareto trade-offs
  method      text NOT NULL                 -- 'tree_shap', 'propagation_path', 'pareto_tradeoff'
);
