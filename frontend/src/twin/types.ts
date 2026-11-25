/** API version 2 of the digital twin. The published contract is docs/api/twin-v2.schema.json;
 *  types.test.ts checks this file against it. */

export const STATUSES = ["healthy", "warning", "overloaded", "failed", "recovering", "unknown"] as const;
export type Status = (typeof STATUSES)[number];

export const COMPONENT_TYPES = [
  "microservice", "database", "server", "network", "load_balancer", "gateway", "monitoring",
] as const;
export type ComponentType = (typeof COMPONENT_TYPES)[number];

export const LINK_KINDS = [
  "calls", "routes_to", "stores_in", "hosted_on", "connected_to", "monitors",
] as const;
export type LinkKind = (typeof LINK_KINDS)[number];

export type Quality = "complete" | "partial" | "derived" | "none";
export type Metrics = Readonly<Record<string, number>>;

export interface Finding {
  level: "warning" | "overload" | "failure";
  rule: string;
  message: string;
  metric: string | null;
  value: number | null;
  threshold: number | null;
}

export interface DependencyRef {
  id: string;
  link_id: string;
  kind: LinkKind;
  mode: string;
  status: Status;
  link_status: Status;
}

export interface TwinComponent {
  id: string;
  type: ComponentType;
  name: string;
  description: string;
  criticality: number;
  status: Status;
  status_since: string | null;
  previous_status: Status | null;
  reason: string;
  findings: Finding[];
  root_causes: string[];
  metrics: Metrics;
  data_quality: Quality;
  attributes: Record<string, unknown>;
  dependencies: { depends_on: DependencyRef[]; dependents: DependencyRef[] };
  display: {
    layer: string;
    group: string;
    order: number;
    size: number;
    load: number | null;
    attention: boolean;
  };
}

export interface TwinLink {
  id: string;
  source: string;
  target: string;
  kind: LinkKind;
  mode: string;
  weight: number;
  purpose: string;
  status: Status;
  status_since: string | null;
  reason: string;
  active: boolean;
  metrics: Metrics;
  data_quality: Quality;
  display: { flow: number; attention: boolean };
}

export interface Summary {
  overall: Status;
  components: number;
  by_status: Partial<Record<Status, number>>;
  by_type: Partial<Record<ComponentType, number>>;
  links: number;
  links_failed: number;
  needs_attention: string[];
}

export interface Meta {
  api_version: string;
  twin_id: string;
  run_id: string;
  version: number;
  structure_version: number;
  sync: "live" | "stale" | "waiting";
  origin: "measured" | "synthetic" | null;
  last_observation_at: string | null;
  generated_at: string;
}

export interface Transition {
  ts: string;
  version: number;
  object_kind: "component" | "link";
  object_id: string;
  from_status: Status | null;
  to_status: Status;
  reason: string;
  root_causes: string[];
  origin: string | null;
  /** Only in history answers; live ones belong to the current run. */
  run_id?: string;
}

export interface TwinMessage {
  type: "snapshot" | "delta" | "heartbeat";
  base_version: number | null;
  meta: Meta;
  summary: Summary | null;
  components: TwinComponent[];
  links: TwinLink[];
  transitions: Transition[];
}

export interface Catalogue {
  metric_units: Record<string, string>;
  statuses: { status: Status; meaning: string }[];
  layers: string[];
}
