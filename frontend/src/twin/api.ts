import type { Catalogue, Transition } from "./types";
import type { Point } from "./samples";

async function json<T>(url: string, signal?: AbortSignal): Promise<T> {
  const r = await fetch(url, { signal });
  if (!r.ok) throw new Error(`${url.split("?")[0]} answered ${r.status}`);
  return (await r.json()) as T;
}

export const getCatalogue = () => json<Catalogue>("/api/v2/twin/catalogue");

export const getTransitions = (kind: Transition["object_kind"], minutes: number, limit: number) =>
  json<Transition[]>(`/api/v2/twin/history/transitions?kind=${kind}&minutes=${minutes}&limit=${limit}`);

interface HistoryAnswer {
  points: ({ ts: string } & Record<string, unknown>)[];
}

/** Values measured before the page was opened. */
export async function getServiceHistory(
  service: string, metrics: string[], origin: string, minutes: number, signal?: AbortSignal,
): Promise<Map<string, Point[]>> {
  const q = new URLSearchParams({ metrics: metrics.join(","), minutes: String(minutes), step: "2", origin });
  const a = await json<HistoryAnswer>(
    `/monitoring/api/v1/metrics/history/${encodeURIComponent(service)}?${q}`, signal);
  const out = new Map<string, Point[]>(metrics.map((m) => [m, []]));
  for (const p of a.points) {
    const t = Date.parse(p.ts);
    if (!Number.isFinite(t)) continue;
    for (const m of metrics) {
      const v = p[m] as { avg: number } | null;
      if (v) out.get(m)!.push({ t, v: v.avg });
    }
  }
  return out;
}
