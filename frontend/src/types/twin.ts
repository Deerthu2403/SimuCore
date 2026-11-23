// TypeScript mirrors of backend contracts (app/domain/models.py, contracts.py).

export type NodeKind =
  | "gateway" | "service" | "database" | "cache" | "queue"
  | "server" | "load_balancer" | "firewall" | "switch" | "external";

export type NodeLayer = "edge" | "application" | "data" | "compute" | "network";
export type EdgeKind = "calls" | "reads_writes" | "publishes" | "hosted_on" | "routes_through";
export type HealthStatus = "healthy" | "degraded" | "critical" | "down" | "unknown";

export interface InfraNode {
  id: string;
  name: string;
  kind: NodeKind;
  layer: NodeLayer;
  criticality: number;
  capacity: Record<string, number>;
  attributes: Record<string, unknown>;
}

export interface DependencyEdge {
  source: string; // depends on target
  target: string;
  kind: EdgeKind;
  weight: number;
}

export interface Topology {
  version: number;
  nodes: InfraNode[];
  edges: DependencyEdge[];
}

export interface NodeState {
  node_id: string;
  status: HealthStatus;
  metrics: Record<string, number>;
  updated_at: string | null;
}

export interface ImpactedNode {
  node_id: string;
  impact: number;
  depth: number;
  path: string[];
  criticality: number;
}

export interface Explanation {
  summary: string;
  method: string;
  factors: { name: string; value: number | string | null; contribution: number }[];
  evidence: Record<string, unknown>[];
  confidence: number | null;
}

export interface PropagationResponse {
  result: { origin: string[]; method: string; impacted: ImpactedNode[]; business_impact: number };
  explanation: Explanation;
}

export type TwinMessage =
  | { type: "snapshot"; version: number; source: string; topology: Topology; states: NodeState[] }
  | { type: "delta"; version: number; states: NodeState[] };
