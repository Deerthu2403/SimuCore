import { useId, useMemo, useRef, useState, useSyncExternalStore } from "react";
import * as samples from "../twin/samples";
import { clock, formatMetric } from "../twin/format";

const WINDOW_MS = 5 * 60_000;
/** Measurements come every 2 s. A longer hole is a hole, not a line. */
const GAP_MS = 9_000;

/** The smallest round number at or above the maximum. */
export function niceMax(v: number): number {
  if (!(v > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const f of [1, 2, 2.5, 5, 10]) if (v <= f * p * 1.0001) return f * p;
  return 10 * p;
}

export function segments(points: readonly samples.Point[]): samples.Point[][] {
  const out: samples.Point[][] = [];
  let last = -Infinity;
  for (const p of points) {
    if (p.t - last > GAP_MS) out.push([]);
    out[out.length - 1].push(p);
    last = p.t;
  }
  return out;
}

export function Sparkline({ id, metric, units, label, width = 148, height = 44 }: {
  id: string; metric: string; units: Record<string, string>; label: string; width?: number; height?: number;
}) {
  const version = useSyncExternalStore(samples.subscribe, samples.currentVersion);
  const svg = useRef<SVGSVGElement>(null);
  const [at, setAt] = useState<number | null>(null);
  const clip = useId();
  const all = samples.get(id, metric);
  const { shown, t0, t1, top, x, y, parts } = useMemo(() => {
    const t1 = all.length ? all[all.length - 1].t : 0;
    const t0 = t1 - WINDOW_MS;
    const shown = all.filter((p) => p.t >= t0);
    const top = niceMax(Math.max(0, ...shown.map((p) => p.v)));
    const x = (t: number) => 2 + ((t - t0) / WINDOW_MS) * (width - 8);
    const y = (v: number) => height - 3 - (v / top) * (height - 9);
    return { shown, t0, t1, top, x, y, parts: segments(shown) };
    // `version` is what changes when points are added in place
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [all, version, width, height]);

  if (shown.length < 2) return <div className="spark-empty" style={{ height }}>collecting…</div>;

  const line = (s: samples.Point[]) => s.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)} ${y(p.v).toFixed(1)}`).join("");
  const area = (s: samples.Point[]) =>
    `${line(s)}L${x(s[s.length - 1].t).toFixed(1)} ${height - 3}L${x(s[0].t).toFixed(1)} ${height - 3}Z`;
  const last = shown[shown.length - 1];
  const focus = at === null ? null : shown.reduce((a, b) => (Math.abs(b.t - at) < Math.abs(a.t - at) ? b : a));
  const move = (e: React.PointerEvent) => {
    const r = svg.current!.getBoundingClientRect();
    setAt(t0 + ((e.clientX - r.left) / r.width) * WINDOW_MS);
  };
  const fmt = (v: number) => formatMetric(metric, v, units).filter(Boolean).join(" ");
  return (
    <div className="spark">
      <svg
        ref={svg} viewBox={`0 0 ${width} ${height}`} height={height} role="img" preserveAspectRatio="none"
        aria-label={`${label}, last five minutes, now ${fmt(last.v)}, scale to ${fmt(top)}`}
        onPointerMove={move} onPointerLeave={() => setAt(null)}
      >
        <clipPath id={clip}><rect x={0} y={0} width={width} height={height} /></clipPath>
        <g className="grid">
          <line x1={0} x2={width} y1={y(0)} y2={y(0)} />
          <line x1={0} x2={width} y1={y(top)} y2={y(top)} />
        </g>
        <g clipPath={`url(#${clip})`}>
          {parts.map((s) => (
            <g key={s[0].t}>
              {s.length > 1 && <path className="area" d={area(s)} />}
              <path className="line" d={line(s)} />
            </g>
          ))}
        </g>
        {focus && <line className="cross" x1={x(focus.t)} x2={x(focus.t)} y1={0} y2={height} />}
      </svg>
      {/* round marks outside the stretched drawing */}
      <i className="end" style={{ left: `${(x((focus ?? last).t) / width) * 100}%`, top: y((focus ?? last).v) }} />
      <span className="scale">{fmt(top)}</span>
      {focus && (
        <span className="tip" style={focus.t > (t0 + t1) / 2 ? { right: "40%" } : { left: "40%" }}>
          <b>{fmt(focus.v)}</b> {clock(focus.t)}
        </span>
      )}
    </div>
  );
}

/** Minimum, mean and maximum of what is shown: the same data as the curve, as numbers. */
export function useStats(id: string, metric: string) {
  const version = useSyncExternalStore(samples.subscribe, samples.currentVersion);
  return useMemo(() => {
    const all = samples.get(id, metric);
    if (!all.length) return null;
    const t0 = all[all.length - 1].t - WINDOW_MS;
    const v = all.filter((p) => p.t >= t0).map((p) => p.v);
    return { min: Math.min(...v), max: Math.max(...v), mean: v.reduce((a, b) => a + b, 0) / v.length, n: v.length };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, metric, version]);
}
