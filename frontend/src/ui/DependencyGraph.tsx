import { useMemo, useState } from "react";
import { reach, rows as rowsOf } from "../twin/graph";
import { iconOf } from "../twin/icons";
import { STATUS } from "../twin/format";
import { useTwin } from "../twin/store";
import { useView } from "../twin/view";
import { shortName } from "../scene/Labels";
import { Icon } from "./Icon";

const W = 336;
const CHIP_W = 104;
const CHIP_H = 34;
const GAP = 8;
const PER_ROW = 3;

/** What depends on the centre above it, what it depends on below it. Click a box to continue there. */
export function DependencyGraph({ center }: { center: string }) {
  const components = useTwin((s) => s.mirror?.components);
  const links = useTwin((s) => s.mirror?.links);
  const ids = useTwin((s) => s.mirror?.linkIds);
  const [over, setOver] = useState<string | null>(null);
  const { select, hover } = useView.getState();
  // the shape depends on the structure only, the colours on every update
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const r = useMemo(() => (links ? reach(center, links) : null), [center, ids]);
  const g = useMemo(() => {
    if (!r) return null;
    const rows = new Map<number, string[]>();
    for (const [id, s] of rowsOf(center, r, links!)) (rows.get(s) ?? rows.set(s, []).get(s)!).push(id);
    const pos = new Map<string, { x: number; y: number }>();
    let y = 14;
    const marks: { y: number; text: string }[] = [];
    for (const s of [...rows.keys()].sort((a, b) => a - b)) {
      const list = rows.get(s)!.sort();
      const first = s === Math.min(...rows.keys());
      if (s < 0 && first) marks.push({ y: y - 4, text: "depends on the selected component ↓" });
      if (s === 1) marks.push({ y: y - 4, text: "the selected component depends on ↓" });
      for (let i = 0; i < list.length; i += PER_ROW) {
        const line = list.slice(i, i + PER_ROW);
        const width = line.length * CHIP_W + (line.length - 1) * GAP;
        line.forEach((id, k) => pos.set(id, { x: (W - width) / 2 + k * (CHIP_W + GAP), y }));
        y += CHIP_H + (i + PER_ROW < list.length ? 6 : 0);
      }
      y += 26;
    }
    return { pos, height: y - 14, marks };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [r]);
  if (!r || !g || !components || !links) return null;
  if (r.steps.size === 1) return <p className="empty">No dependencies.</p>;

  const related = over ? new Set([over, ...[...r.links].flatMap((l) => {
    const k = links[l];
    return k && k.source === over ? [k.target] : k && k.target === over ? [k.source] : [];
  })]) : null;

  return (
    <div className="graph" data-testid="dependency-graph">
      <svg viewBox={`0 0 ${W} ${g.height}`} width="100%" role="group" aria-label="Dependency graph">
        {[...r.links].map((l) => {
          const k = links[l];
          const a = k && g.pos.get(k.source);
          const b = k && g.pos.get(k.target);
          if (!a || !b) return null;
          const x0 = a.x + CHIP_W / 2;
          const y0 = a.y + CHIP_H;
          const x1 = b.x + CHIP_W / 2;
          const y1 = b.y;
          const bend = Math.max(14, Math.abs(y1 - y0) * 0.45);
          const faded = over !== null && k.source !== over && k.target !== over;
          const bad = k.status !== "healthy";
          return (
            <path
              key={l} d={`M${x0} ${y0}C${x0} ${y0 + bend} ${x1} ${y1 - bend} ${x1} ${y1}`} className="graph-edge"
              stroke={bad ? STATUS[k.status].color : k.active ? "#3987e5" : "#3a4656"} opacity={faded ? 0.12 : 1}
              strokeWidth={bad ? 2 : k.active ? 1.6 : 1} strokeDasharray={k.status === "failed" ? "5 4" : k.status === "unknown" ? "2 4" : undefined}
            />
          );
        })}
        {g.marks.map((m) => <text key={m.text} x={W / 2} y={m.y} className="graph-mark">{m.text}</text>)}
        {[...g.pos].map(([id, p]) => {
          const c = components[id];
          if (!c) return null;
          const s = STATUS[c.status];
          const name = shortName(c.name);
          return (
            <g
              key={id} className="graph-node" data-node={id} transform={`translate(${p.x} ${p.y})`} tabIndex={0} role="button"
              aria-label={`${c.name}, ${s.label}`} opacity={related && !related.has(id) ? 0.3 : 1}
              onClick={() => select({ kind: "component", id }, { fly: true })}
              onKeyDown={(e) => { if (e.key === "Enter") select({ kind: "component", id }, { fly: true }); }}
              onMouseEnter={() => { setOver(id); hover(id); }} onMouseLeave={() => { setOver(null); hover(null); }}
            >
              <title>{`${c.name}: ${s.label}`}</title>
              <rect width={CHIP_W} height={CHIP_H} rx={6} className={id === center ? "center" : ""} stroke={s.color} />
              <svg x={6} y={9} width={16} height={16}><Icon name={iconOf(c)} size={16} /></svg>
              <text x={26} y={14.5}>{name.length > 12 ? name.slice(0, 11) + "…" : name}</text>
              <svg x={25} y={18} width={11} height={11}><Icon name={s.icon} size={11} color={s.color} /></svg>
              <text x={38} y={27} className="graph-status">{s.label}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
