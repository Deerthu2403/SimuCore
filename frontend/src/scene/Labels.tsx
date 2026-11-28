import { memo, useMemo } from "react";
import { useFrame, useThree } from "@react-three/fiber";
import { Ray, Vector3 } from "three";
import { useTwin, useUnits } from "../twin/store";
import { useView } from "../twin/view";
import { AI_COLOR, isAnomalous, percent, riskOf, topWarning } from "../twin/ai";
import { STATUS, text } from "../twin/format";
import { describe, IMPACT_COLOR, impactOf, ROLE, useImpact } from "../twin/impact";
import { Icon } from "../ui/Icon";
import { world } from "./world";

/** Names above the components, as HTML so that they stay sharp and readable. Positions are written
 *  straight to the elements once per frame; React only renders them when their text changes. */
const MAX_LABELS = 60;
const elements = new Map<string, HTMLDivElement>();
const zoneElements = new Map<string, HTMLDivElement>();
const V = new Vector3();
const RAY = new Ray();
const hiddenZones = new Set<string>();
let turn = 0;

export const shortName = (n: string) => n.replace(/ Service$/, "");

export function labelState(id: string) {
  const e = elements.get(id);
  return e ? { text: e.innerText, shown: e.style.visibility !== "hidden", status: e.dataset.status } : null;
}

function useLabelIds(): string[] {
  const ids = useTwin((s) => s.mirror?.componentIds);
  const attention = useTwin((s) => s.mirror?.summary.needs_attention);
  const selected = useView((s) => s.selected);
  const hovered = useView((s) => s.hovered);
  return useMemo(() => {
    if (!ids) return [];
    if (ids.length <= MAX_LABELS) return [...ids];
    const some = new Set<string>();
    if (selected?.kind === "component") some.add(selected.id);
    if (hovered) some.add(hovered);
    for (const i of attention ?? []) if (some.size < MAX_LABELS / 2) some.add(i);
    for (const i of ids) if (some.size < MAX_LABELS) some.add(i);
    return [...some];
  }, [ids, attention, selected, hovered]);
}

/** Moves the labels. Priority: what the user points at, then trouble, then nearness. */
export function LabelMover() {
  const { camera, size } = useThree();

  useFrame(() => {
    const zones = world.layout.zones;
    const view = useView.getState();
    const taken: [number, number, number, number][] = [];
    const list: { id: string; e: HTMLDivElement; x: number; y: number; z: number; rank: number }[] = [];
    for (const [id, e] of elements) {
      const n = world.nodes.get(id);
      if (!n) continue;
      V.set(n.x, n.size.h + 1.18 + (n.status === "healthy" ? 0 : 0.85), n.z).project(camera);
      const x = ((V.x + 1) / 2) * size.width;
      const y = ((1 - V.y) / 2) * size.height;
      const point = id === view.hovered || (view.selected?.kind === "component" && view.selected.id === id);
      const off = V.z > 1 || x < -60 || y < -20 || x > size.width + 60 || y > size.height + 20;
      if (off || n.visible < 0.3 || (!view.labels && !point)) {
        e.style.visibility = "hidden";
        continue;
      }
      list.push({ id, e, x, y, z: V.z, rank: point ? 100 : STATUS[n.status].rank * 10 + (n.risk > 0 ? 35 : 0) + (n.anomaly > 0 ? 5 : 0) + n.role * 6 });
    }
    list.sort((a, b) => b.rank - a.rank || a.z - b.z);
    for (const l of list) {
      const w = l.e.offsetWidth || 110;
      const h = l.e.offsetHeight || 20;
      const left = Math.max(2, Math.min(l.x - w / 2, size.width - w - 2));
      const r: [number, number, number, number] = [left, l.y - h, left + w, l.y];
      const clash = () => taken.some((t) => r[0] < t[2] + 3 && r[2] > t[0] - 3 && r[1] < t[3] + 2 && r[3] > t[1] - 2);
      // a little higher or lower is better than not at all
      let free = !clash();
      for (const shift of [-1, 1.2, -2]) {
        if (free || l.rank >= 100) break;
        r[1] = l.y - h + shift * (h + 3);
        r[3] = r[1] + h;
        free = !clash();
      }
      if (!free && l.rank < 100) {
        l.e.style.visibility = "hidden";
        continue;
      }
      taken.push(r);
      l.e.style.visibility = "visible";
      l.e.style.zIndex = String(Math.round(l.rank * 1000 + (1 - l.z) * 100));
      l.e.style.transform = `translate(${Math.round(r[0])}px, ${Math.round(r[1])}px)`;
    }
    const showZones = view.labels && view.mode === "normal";
    turn++;
    for (const z of zones) {
      const e = zoneElements.get(z.id);
      if (!e) continue;
      // a name on the floor must not be written across the rack that stands in front of it
      if (turn % 8 === 0) {
        hiddenZones.delete(z.id);
        for (const dx of [0.2, 1.8]) {
          RAY.origin.copy(camera.position);
          RAY.direction.set(z.x0 + dx, 0.02, z.z1 + 0.3).sub(camera.position).normalize();
          if (world.pick(RAY)) hiddenZones.add(z.id);
        }
      }
      V.set(z.x0 + 0.2, 0, z.z1 + 0.15).project(camera);
      const zx = ((V.x + 1) / 2) * size.width;
      const zy = ((1 - V.y) / 2) * size.height;
      const zr: [number, number, number, number] = [zx, zy, zx + 110, zy + 16];
      const on = showZones && V.z < 1 && !hiddenZones.has(z.id)
        && !taken.some((t) => zr[0] < t[2] && zr[2] > t[0] && zr[1] < t[3] && zr[3] > t[1]);
      if (on) taken.push(zr);
      e.style.visibility = on ? "visible" : "hidden";
      if (on) e.style.transform = `translate(${Math.round(((V.x + 1) / 2) * size.width)}px, ${Math.round(((1 - V.y) / 2) * size.height)}px)`;
    }
  });

  return null;
}

/** The elements, in the page next to the canvas. */
export function Labels() {
  const ids = useLabelIds();
  return (
    <div className="label-layer" aria-hidden="true">
      {world.layout.zones.map((z) => (
        <div
          key={z.id} className="zone-label" style={{ visibility: "hidden" }}
          ref={(e) => { if (e) zoneElements.set(z.id, e); else zoneElements.delete(z.id); }}
        >
          {z.label}
        </div>
      ))}
      {ids.map((id) => <Label key={id} id={id} />)}
    </div>
  );
}

const Label = memo(function Label({ id }: { id: string }) {
  const name = useTwin((s) => s.mirror?.components[id]?.name);
  const status = useTwin((s) => s.mirror?.components[id]?.status);
  const anomaly = useTwin((s) => { const c = s.mirror?.components[id]; return !!c && isAnomalous(c); });
  const risk = useTwin((s) => {
    const c = s.mirror?.components[id];
    const w = c && topWarning(c);
    return w ? `${w.target} ${percent(riskOf(c))} in ${w.horizon_s / 60} min` : "";
  });
  const near = useView((s) => s.hovered === id || (s.selected?.kind === "component" && s.selected.id === id));
  const impact = useImpactLine(id);
  if (!name || !status) return null;
  return (
    <div
      className={`label ${near ? "near" : ""}`} data-status={status} data-id={id} style={{ visibility: "hidden" }}
      ref={(e) => { if (e) elements.set(id, e); else elements.delete(id); }}
    >
      <span className="label-line">
        <Icon name={STATUS[status].icon} size={12} color={STATUS[status].color} />
        <b>{shortName(name)}</b>
        {status !== "healthy" && <em>{STATUS[status].label}</em>}
      </span>
      {(anomaly || risk) && (
        <span className="label-ai">
          {anomaly && <span><Icon name="anomaly" size={11} color={AI_COLOR.anomaly} /> anomaly</span>}
          {risk && <span><Icon name="forecast" size={11} color={AI_COLOR.risk} /> {risk}</span>}
        </span>
      )}
      {impact && (
        <span className="label-impact" data-role={impact.role} style={{ color: IMPACT_COLOR }}>
          <Icon name="graph" size={11} color={IMPACT_COLOR} /> {impact.text}
        </span>
      )}
      {near && <LabelNumbers id={id} />}
    </div>
  );
});

/** "predicted impact: high 60 %, 1 step from Payment Gateway"; empty for the failure itself,
 *  whose status already says it. */
function useImpactLine(id: string): { role: string; text: string } | null {
  const sim = useImpact((s) => s.simulation);
  const line = useTwin((s) => {
    const m = s.mirror;
    const c = m?.components[id];
    const i = c && impactOf(c, sim);
    if (!i || !m) return "";
    const name = (x: string) => m.components[x]?.name ?? x;
    if (i.role === "origin") return sim ? "origin|simulated failure" : "";
    return `${i.role}|${ROLE[i.role].label}: ${describe(i, name)}`;
  });
  if (!line) return null;
  const [role, text] = line.split("|");
  return { role, text };
}

function LabelNumbers({ id }: { id: string }) {
  const m = useTwin((s) => s.mirror?.components[id]?.metrics);
  const units = useUnits();
  if (!m) return null;
  const shown = ["cpu_pct", "mem_pct", "latency_ms", "rps", "ingress_rps", "calls_per_s", "hosted_cpu_pct", "db_pool_in_use", "connections"]
    .filter((k) => k in m).slice(0, 4);
  if (!shown.length) return <span className="label-numbers">no measurements</span>;
  const tag: Record<string, string> = {
    cpu_pct: "CPU", mem_pct: "RAM", latency_ms: "p95", rps: "", ingress_rps: "", calls_per_s: "", hosted_cpu_pct: "CPU",
    db_pool_in_use: "pool", connections: "conn.",
  };
  return <span className="label-numbers">{shown.map((k) => `${tag[k]} ${text(k, m[k], units)}`.trim()).join("  ·  ")}</span>;
}
