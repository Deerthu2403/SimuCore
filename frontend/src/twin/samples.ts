import type { Mirror } from "./mirror";
import type { Insight } from "./types";

/** Recent values of every metric, kept outside React: new points arrive every 2 s for every
 *  component, and only the charts on screen need to know. */
export interface Point {
  t: number;
  v: number;
}

const KEEP_MS = 10 * 60_000;
const series = new Map<string, Point[]>();
const listeners = new Set<() => void>();
let version = 0;
let lastT = 0;

const key = (id: string, metric: string) => `${id}\u0000${metric}`;

/** Called after every message. A value that is absent leaves a gap, never a zero. */
export function record(m: Mirror): void {
  if (m.meta.sync !== "live" || !m.meta.last_observation_at) return;
  const t = Date.parse(m.meta.last_observation_at);
  if (!(t > lastT)) return;
  lastT = t;
  for (const id of m.componentIds) {
    push(id, t, m.components[id].metrics);
    push(id, t, opinions(m.components[id].insight));
  }
  for (const id of m.linkIds) push(id, t, m.links[id].metrics);
  version++;
  listeners.forEach((f) => f());
}

/** The opinions of the AI as curves, next to the measurements. */
function opinions(i: Insight | null | undefined): Record<string, number> {
  const out: Record<string, number> = {};
  if (i?.anomaly) out.ai_anomaly = i.anomaly.score;
  for (const r of i?.risks ?? []) if (r.probability !== null) out[`ai_risk_${r.target}`] = r.probability;
  return out;
}

function push(id: string, t: number, metrics: Readonly<Record<string, number>>): void {
  for (const metric in metrics) {
    const k = key(id, metric);
    let s = series.get(k);
    if (!s) series.set(k, (s = []));
    s.push({ t, v: metrics[metric] });
    while (s.length && s[0].t < t - KEEP_MS) s.shift();
  }
}

/** Adds older points (from the monitoring history) in front of those received live. */
export function prefill(id: string, metric: string, points: Point[]): void {
  const k = key(id, metric);
  const live = series.get(k) ?? [];
  const first = live.length ? live[0].t : Infinity;
  const older = points.filter((p) => p.t < first - 500 && Number.isFinite(p.v)).sort((a, b) => a.t - b.t);
  if (!older.length) return;
  series.set(k, [...older, ...live]);
  version++;
  listeners.forEach((f) => f());
}

export const get = (id: string, metric: string): readonly Point[] => series.get(key(id, metric)) ?? [];
export const currentVersion = () => version;
export function subscribe(f: () => void): () => void {
  listeners.add(f);
  return () => listeners.delete(f);
}

/** Another run or origin: its values do not belong in the same curve. */
export function clear(): void {
  series.clear();
  lastT = 0;
  version++;
  listeners.forEach((f) => f());
}
