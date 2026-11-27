import { create } from "zustand";
import type { TwinComponent } from "./types";

/** History and quality of the AI service. What it thinks now comes with the twin itself
 *  (component.insight); this is only what has to be looked up. */
export interface StoredWarning {
  id: string;
  node_id: string;
  target: "degradation" | "failure";
  opened_at: string;
  closed_at: string | null;
  horizon_s: number;
  probability: number;
  peak: number;
  reasons: { text: string; metric: string; contribution: number }[];
  outcome: "open" | "came_true" | "did_not_happen" | "unresolved";
  lead_s: number | null;
}

export interface StoredAnomaly {
  id: string;
  node_id: string;
  opened_at: string;
  closed_at: string | null;
  score: number;
  level: "unusual" | "abnormal";
  condition: string;
  contributions: { metric: string; share: number; deviation: number }[];
}

interface Judged {
  precision: number | null;
  recall: number | null;
  f1: number | null;
  roc_auc: number | null;
  brier?: number;
}
interface Announced {
  failures: number;
  announced: number;
  alarms: number;
  false_alarms: number;
  lead_s_median: number | null;
  false_alarms_per_service_hour: number | null;
}

export interface ModelInfo {
  version: string;
  trained_at: string;
  trained_on: string;
  horizon_s: number;
  thresholds: Record<string, number>;
  conditions: Record<string, string>;
  importance: Record<string, Record<string, number>>;
  feature_descriptions: Record<string, string>;
  anomaly_detection: string;
  failure_prediction: string;
  test: { anomaly: Judged } & Record<"degradation" | "failure", { xgboost: Judged; events: Announced }>;
  /** Experiments on the real simulated bank; null if none were made with this model. */
  validation: {
    made_at: string;
    recordings: number;
    anomaly: Judged;
    degradation: { rows: Judged; events: Announced };
    failure: { rows: Judged; events: Announced };
  } | null;
}

export interface Performance {
  history: Record<"degradation" | "failure", {
    predictions: number;
    events: number;
    brier: number | null;
    warnings: Partial<Record<StoredWarning["outcome"], number>>;
    lead_s_median: number | null;
  }>;
}

interface Store {
  model: ModelInfo | null;
  warnings: StoredWarning[];
  anomalies: StoredAnomaly[];
  performance: Performance | null;
  state: "loading" | "ready" | "unavailable";
}

export const useAi = create<Store>(() => ({ model: null, warnings: [], anomalies: [], performance: null, state: "loading" }));

async function json<T>(path: string): Promise<T> {
  const r = await fetch(`/ai/api/v1/ai/${path}`);
  if (!r.ok) throw new Error(`${path.split("?")[0]} answered ${r.status}`);
  return (await r.json()) as T;
}

let loading = false;
export async function loadAi(origin: string): Promise<void> {
  if (loading) return;
  loading = true;
  try {
    const model = useAi.getState().model ?? (await json<ModelInfo>("model"));
    useAi.setState({ model });
    const [warnings, anomalies, performance] = await Promise.all([
      json<StoredWarning[]>(`warnings?minutes=720&limit=200&origin=${origin}`),
      json<StoredAnomaly[]>(`anomalies?minutes=720&limit=200&origin=${origin}`),
      json<Performance>(`performance?minutes=1440&origin=${origin}`),
    ]);
    useAi.setState({ warnings, anomalies, performance, state: "ready" });
  } catch {
    useAi.setState({ state: "unavailable" });
  } finally {
    loading = false;
  }
}

/** Anomaly: unsupervised, about now. Risk: supervised, about the next two minutes. */
export const AI_COLOR = { anomaly: "#b18cff", risk: "#ff86c8" } as const;

export const METRIC_NAME: Record<string, string> = {
  cpu: "CPU", memory: "Memory", latency: "Latency", errors: "Error rate", requests: "Requests per second",
  network: "Network traffic", database: "Database response time", connections: "Active connections",
  cpu_per_request: "CPU per request", bytes_per_request: "Bytes per request", queue: "Waiting requests",
  in_flight: "Busy worker slots", pool: "Database connections", dependency: "A service it depends on",
};
export const metricName = (m: string) => METRIC_NAME[m] ?? m;

export const CONDITION: Record<string, string> = {
  normal: "nothing unusual", traffic: "unusual traffic", resources: "unusual CPU or memory",
  latency: "unusual latency", errors: "unusual errors", database: "unusual database time",
  connections: "unusual connections",
};

export const isAnomalous = (c: Pick<TwinComponent, "insight">) => !!c.insight?.anomaly?.since;

/** The warning to show: failure before degradation, the more probable first. */
export function topWarning(c: Pick<TwinComponent, "insight">) {
  const w = c.insight?.warnings ?? [];
  return [...w].sort((a, b) => (a.target === b.target ? b.peak - a.peak : a.target === "failure" ? -1 : 1))[0] ?? null;
}

/** Highest current probability, with what it is the probability of; null if nothing is predicted. */
export function topRisk(c: Pick<TwinComponent, "insight">) {
  const r = (c.insight?.risks ?? []).filter((x) => x.probability !== null);
  return r.sort((a, b) => b.probability! - a.probability!)[0] ?? null;
}

/** How near the risk of a target is to its warning threshold: for the 3D marker, 0 to 1. */
export function riskOf(c: Pick<TwinComponent, "insight">): number {
  const w = topWarning(c);
  if (!w) return 0;
  const now = c.insight?.risks.find((r) => r.target === w.target)?.probability;
  return now ?? w.peak;
}

export const percent = (p: number) => `${p >= 0.995 ? "> 99" : p < 0.01 ? "< 1" : Math.round(p * 100)} %`;
export const TARGET: Record<string, string> = { degradation: "degradation", failure: "failure" };
