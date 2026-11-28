import { create } from "zustand";
import type { ImpactView, TwinComponent, TwinLink } from "./types";

/** Failure propagation. What the twin estimates now comes with every component (component.impact)
 *  and link (display.propagation); this module adds what has to be asked for: a simulated failure,
 *  the ranking of critical components and the stored events. */

/** Not a status colour and not one of the AI's: the propagation has its own. */
export const IMPACT_COLOR = "#2ed3c0";

export type ImpactLevel = ImpactView["level"];
export const LEVEL: Record<ImpactLevel | "none", { label: string; meaning: string }> = {
  failed: { label: "failed", meaning: "the failure itself" },
  severe: { label: "severe", meaning: "likely to fail as well" },
  high: { label: "high", meaning: "a large part of its work is likely to fail" },
  moderate: { label: "moderate", meaning: "some of its work may fail" },
  low: { label: "low", meaning: "little effect expected" },
  none: { label: "none", meaning: "no effect expected" },
};
export const ROLE: Record<ImpactView["role"], { label: string; meaning: string }> = {
  origin: { label: "failed", meaning: "a confirmed failure that nothing else explains" },
  confirmed: { label: "confirmed impact", meaning: "affected, and its own measurements show trouble" },
  predicted: { label: "predicted impact", meaning: "affected according to the dependency graph only" },
};

export interface ReportRow {
  id: string;
  name: string;
  type: string;
  impact: number;
  level: Exclude<ImpactLevel, "failed">;
  meaning: string;
  criticality: number;
  score: number;
  steps: number;
  chain: string[];
  chain_ids: string[];
  origins: string[];
  status: string | null;
  confirmed: boolean;
}

export interface Report {
  title: string;
  origins: { id: string; name: string; criticality: number; status: string | null }[];
  graph: string;
  components: number;
  dependencies: number;
  reachable: number;
  affected: number;
  affected_severe: number;
  affected_any: number;
  expected_affected: number;
  business_impact: number;
  level: ImpactLevel | "none";
  impacts: ReportRow[];
  not_affected_dependencies: { id: string; name: string; kind: string; loses_calls_per_s: number | null; of: string }[];
  method: string;
}

export interface Simulation extends Report {
  what_if: true;
  links: Record<string, { impact: number; step: number }>;
}

export interface Critical {
  id: string;
  name: string;
  type: string;
  criticality: number;
  affected: number;
  severe: number;
  expected: number;
  business_impact: number;
  single_point: boolean;
}

export interface Outcome {
  judged: boolean;
  observed: string[];
  hits: string[];
  missed: string[];
  not_observed: string[];
  precision: number | null;
  recall: number | null;
}

export interface PropagationEvent {
  id: string;
  opened_at: string;
  closed_at: string | null;
  origins: string[];
  predicted: string[];
  outcome: Outcome | null;
  report: Report;
}

interface Store {
  simulation: Simulation | null;
  simulating: string[];
  critical: Critical[];
  events: PropagationEvent[];
  /** db: stored events; live: only this run of the twin (no database). */
  source: "db" | "live" | null;
  error: string | null;
}

export const useImpact = create<Store>(() => ({
  simulation: null, simulating: [], critical: [], events: [], source: null, error: null,
}));

const BASE = "/api/v2/twin/propagation";

async function json<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url.split("?")[0]} answered ${r.status}`);
  return (await r.json()) as T;
}

export const markdownUrl = (ids: string[]) => `${BASE}/analyze?failed=${ids.map(encodeURIComponent).join(",")}&format=markdown`;

/** What would happen if these components failed. Changes nothing in the twin. */
export async function simulate(ids: string[]): Promise<void> {
  if (!ids.length) return clearSimulation();
  useImpact.setState({ simulating: ids, error: null });
  try {
    const simulation = await json<Simulation>(`${BASE}/analyze?failed=${ids.map(encodeURIComponent).join(",")}`);
    // a later request wins
    if (useImpact.getState().simulating.join() === ids.join()) useImpact.setState({ simulation });
  } catch (e) {
    useImpact.setState({ simulation: null, simulating: [], error: e instanceof Error ? e.message : String(e) });
  }
}

export function clearSimulation(): void {
  useImpact.setState({ simulation: null, simulating: [] });
}

let loading = false;
export async function loadImpact(): Promise<void> {
  if (loading) return;
  loading = true;
  try {
    const critical = await json<{ components: Critical[] }>(`${BASE}/critical?group=bank`);
    let events: PropagationEvent[];
    let source: Store["source"] = "db";
    try {
      events = await json<PropagationEvent[]>(`${BASE}/events?minutes=720&limit=50`);
    } catch {
      events = await json<PropagationEvent[]>(`${BASE}/events/live`);
      source = "live";
    }
    useImpact.setState({ critical: critical.components, events, source, error: null });
  } catch (e) {
    useImpact.setState({ error: e instanceof Error ? e.message : String(e) });
  } finally {
    loading = false;
  }
}

/** The impact a simulation gives a component, in the shape the twin uses. */
export function simulatedImpact(sim: Simulation, id: string): ImpactView | null {
  if (sim.origins.some((o) => o.id === id)) {
    return { role: "origin", impact: 1, level: "failed", score: 0, steps: 0, chain: [id], links: [], origins: [id] };
  }
  const r = sim.impacts.find((x) => x.id === id);
  if (!r) return null;
  const links = r.chain_ids.slice(1).map((c, i) => `${c}->${r.chain_ids[i]}`);
  return {
    role: "predicted", impact: r.impact, level: r.level, score: r.score, steps: r.steps,
    chain: r.chain_ids, links, origins: r.origins,
  };
}

/** What the scene shows: the simulation while there is one, otherwise the twin's own estimate. */
export function impactOf(c: TwinComponent, sim: Simulation | null): ImpactView | null {
  return sim ? simulatedImpact(sim, c.id) : c.impact;
}

export function propagationOf(k: TwinLink, sim: Simulation | null): { impact: number; step: number | null } {
  if (sim) {
    const p = sim.links[k.id];
    return p ? { impact: p.impact, step: p.step } : { impact: 0, step: null };
  }
  return { impact: k.display.propagation ?? 0, step: k.display.propagation_step ?? null };
}

/** Shader code: 0 none, 1 predicted, 2 confirmed, 3 origin. */
export const ROLE_CODE: Record<ImpactView["role"], number> = { predicted: 1, confirmed: 2, origin: 3 };

export const pct = (x: number) => `${Math.round(x * 100)} %`;

/** "high, 60 %, 1 step from Payment Gateway" */
export function describe(i: ImpactView, name: (id: string) => string): string {
  if (i.role === "origin") return "failure origin";
  const steps = `${i.steps} step${i.steps === 1 ? "" : "s"}`;
  return `${LEVEL[i.level].label} ${pct(i.impact)}, ${steps} from ${name(i.chain[0]).replace(/ Service$/, "")}`;
}
