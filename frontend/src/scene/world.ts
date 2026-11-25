import { Box3, Color, Ray, Vector3 } from "three";
import { STATUS } from "../twin/format";
import type { Mirror } from "../twin/mirror";
import type { ComponentType, LinkKind, Status, TwinComponent } from "../twin/types";
import type { Reach } from "../twin/graph";
import { computeLayout, dependencyLayout, scaleOf, SIZE, type Layout, type Size } from "./layout";

/** Numbers the shaders understand. */
export const STATUS_CODE: Record<Status, number> = {
  healthy: 0, warning: 1, overloaded: 2, failed: 3, recovering: 4, unknown: 5,
};
export const LINK_CODE: Record<LinkKind, number> = {
  calls: 0, routes_to: 1, stores_in: 2, hosted_on: 3, connected_to: 4, monitors: 5,
};
/** Links that carry requests; the others are cables, shown on demand. */
export const FLOW_KINDS: ReadonlySet<LinkKind> = new Set(["calls", "routes_to", "stores_in"]);

const COLORS = Object.fromEntries(
  (Object.keys(STATUS) as Status[]).map((s) => [s, new Color(STATUS[s].color)]),
) as Record<Status, Color>;

/** What is drawn for one component. ``to*`` is where it is heading; the rest is on screen now. */
export interface Node {
  id: string;
  type: ComponentType;
  seed: number;
  scale: number;
  size: Size;
  x: number;
  z: number;
  toX: number;
  toZ: number;
  status: Status;
  color: Color;
  load: number;
  toLoad: number;
  visible: number;
  toVisible: number;
  highlight: number;
  toHighlight: number;
  /** Seconds (world clock) when the status last changed; drives the shock wave. */
  flashAt: number;
  bankService: string;
}

export interface Wire {
  id: string;
  index: number;
  kind: LinkKind;
  source: Node;
  target: Node;
  status: Status;
  flow: number;
  toFlow: number;
  failing: number;
  phase: number;
  visible: number;
  toVisible: number;
  highlight: number;
  toHighlight: number;
  /** Control points: start, two handles, end. */
  p: Float32Array;
}

export interface ViewInput {
  reach: Reach | null;
  mode: "normal" | "isolate" | "dependencies";
  selected: string | null;
  selectedLink: string | null;
  hovered: string | null;
  hiddenTypes: ReadonlySet<ComponentType>;
  statusFilter: Status | null;
  structural: boolean;
}

const ease = (from: number, to: number, dt: number, rate: number) => {
  const next = from + (to - from) * (1 - Math.exp(-dt * rate));
  return Math.abs(next - to) < 1e-3 ? to : next;
};

function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return (h >>> 0) / 4294967296;
}

/** The scene's own state, animated outside React. Only ``sync`` and ``view`` put information in;
 *  ``step`` moves what is on screen towards it. */
export class World {
  nodes = new Map<string, Node>();
  wires: Wire[] = [];
  layout: Layout = { places: new Map(), zones: [], bounds: { x0: 0, z0: 0, x1: 0, z1: 0 } };
  clock = 0;
  /** Changes whenever objects are added or removed. */
  structure = 0;
  /** Changes whenever something moved. */
  moved = 0;
  /** True until all animations have arrived. */
  settled = false;
  instant = false;
  private first = true;
  private lastView: ViewInput | null = null;

  sync(m: Mirror): void {
    const sameStructure = this.nodes.size === m.componentIds.length && this.wires.length === m.linkIds.length
      && m.componentIds.every((i) => this.nodes.has(i)) && m.linkIds.every((i, n) => this.wires[n].id === i);
    if (!sameStructure) this.rebuild(m);
    for (const id of m.componentIds) {
      const c = m.components[id];
      const n = this.nodes.get(id)!;
      if (n.status !== c.status) {
        n.status = c.status;
        n.flashAt = this.clock;
      }
      n.toLoad = loadOf(c);
    }
    for (const w of this.wires) {
      const k = m.links[w.id];
      w.status = k.status;
      w.toFlow = k.display.flow;
      w.failing = k.metrics.failure_rate ?? 0;
    }
    this.settled = false;
    if (!sameStructure && this.lastView) this.view(this.lastView);
  }

  private rebuild(m: Mirror): void {
    this.layout = computeLayout(m.componentIds, m.components, m.links);
    const old = this.nodes;
    this.nodes = new Map();
    for (const id of m.componentIds) {
      const c = m.components[id];
      const p = this.layout.places.get(id)!;
      const known = old.get(id);
      const scale = scaleOf(c);
      const s = SIZE[c.type];
      this.nodes.set(id, {
        id, type: c.type, seed: hash(id), scale, size: { w: s.w * scale, h: s.h * scale, d: s.d * scale },
        x: known?.x ?? p.x, z: known?.z ?? p.z, toX: p.x, toZ: p.z, status: c.status,
        color: known?.color ?? COLORS[c.status].clone(), load: known?.load ?? 0, toLoad: loadOf(c),
        visible: known?.visible ?? (this.first ? 1 : 0), toVisible: 1, highlight: known?.highlight ?? 0,
        toHighlight: 0, flashAt: known && known.status === c.status ? known.flashAt : -10,
        bankService: String(c.attributes.service ?? ""),
      });
    }
    const oldWires = new Map(this.wires.map((w) => [w.id, w]));
    this.wires = m.linkIds.map((id, index) => {
      const k = m.links[id];
      const known = oldWires.get(id);
      const structural = !FLOW_KINDS.has(k.kind);
      return {
        id, index, kind: k.kind, source: this.nodes.get(k.source)!, target: this.nodes.get(k.target)!,
        status: k.status, flow: known?.flow ?? 0, toFlow: k.display.flow,
        failing: k.metrics.failure_rate ?? 0, phase: known?.phase ?? hash(id),
        visible: known?.visible ?? 0, toVisible: structural ? 0 : 1, highlight: 0, toHighlight: 0,
        p: new Float32Array(12),
      };
    });
    this.first = false;
    this.structure++;
    this.route();
  }

  view(v: ViewInput): void {
    this.lastView = v;
    const spread = v.mode === "dependencies" && v.reach && v.selected
      ? dependencyLayout(v.selected, v.reach, this.layout) : null;
    for (const n of this.nodes.values()) {
      const p = spread?.get(n.id) ?? this.layout.places.get(n.id)!;
      n.toX = p.x;
      n.toZ = p.z;
      const outside = v.reach && !v.reach.steps.has(n.id);
      const hidden = v.hiddenTypes.has(n.type) && n.id !== v.selected;
      const filtered = v.statusFilter !== null && n.status !== v.statusFilter && n.id !== v.selected;
      n.toVisible = hidden || (outside && n.id !== v.selected) ? 0 : filtered ? 0.16 : 1;
      n.toHighlight = n.id === v.selected ? 1 : n.id === v.hovered ? 0.6 : 0;
    }
    for (const w of this.wires) {
      const structural = !FLOW_KINDS.has(w.kind);
      const touches = (id: string | null) => id !== null && (w.source.id === id || w.target.id === id);
      const ends = Math.min(w.source.toVisible, w.target.toVisible);
      let show = v.reach ? v.reach.links.has(w.id) : !structural || v.structural || touches(v.selected);
      if (w.id === v.selectedLink) show = true;
      const aside = !v.reach && (v.selected !== null || v.selectedLink !== null)
        && !touches(v.selected) && w.id !== v.selectedLink;
      w.toVisible = !show || ends === 0 ? 0 : ends < 1 ? 0.12 : aside ? 0.3 : 1;
      w.toHighlight = w.id === v.selectedLink ? 1 : touches(v.selected) ? 0.6 : touches(v.hovered) ? 0.5 : 0;
    }
    this.settled = false;
  }

  /** One frame. ``motion`` is false when the user asked for reduced motion. */
  step(dt: number, motion: boolean): void {
    this.clock += dt;
    const snap = this.instant || !motion;
    const rate = (r: number) => (snap ? 1e6 : r);
    let moving = false;
    let busy = false;
    for (const n of this.nodes.values()) {
      const x = ease(n.x, n.toX, dt, rate(5));
      const z = ease(n.z, n.toZ, dt, rate(5));
      if (x !== n.x || z !== n.z) moving = true;
      n.x = x;
      n.z = z;
      n.visible = ease(n.visible, n.toVisible, dt, rate(6));
      n.highlight = ease(n.highlight, n.toHighlight, dt, rate(12));
      // unknown is not a number one could pass through
      n.load = n.toLoad < 0 ? -1 : ease(Math.max(0, n.load), n.toLoad, dt, rate(3));
      const to = COLORS[n.status];
      const c = n.color;
      c.r = ease(c.r, to.r, dt, rate(9));
      c.g = ease(c.g, to.g, dt, rate(9));
      c.b = ease(c.b, to.b, dt, rate(9));
      busy ||= n.visible !== n.toVisible || n.highlight !== n.toHighlight || n.load !== n.toLoad
        || c.r !== to.r || c.g !== to.g || c.b !== to.b;
    }
    for (const w of this.wires) {
      w.visible = ease(w.visible, w.toVisible, dt, rate(6));
      w.highlight = ease(w.highlight, w.toHighlight, dt, rate(12));
      w.flow = ease(w.flow, w.toFlow, dt, rate(2));
      // Faster with more traffic; integrated, so that a change of speed never makes particles jump.
      if (motion) w.phase = (w.phase + dt * (0.1 + 0.3 * w.flow)) % 1;
    }
    if (moving) this.route();
    this.settled = !moving && !busy;
  }

  private route(): void {
    for (const w of this.wires) curve(w);
    this.moved++;
  }

  /** The component under a ray, nearest first. */
  pick(ray: Ray): string | null {
    let best: string | null = null;
    let nearest = Infinity;
    for (const n of this.nodes.values()) {
      if (n.visible < 0.5) continue;
      boxOf(n, BOX);
      const hit = ray.intersectBox(BOX, HIT);
      if (!hit) continue;
      const d = hit.distanceToSquared(ray.origin);
      if (d < nearest) {
        nearest = d;
        best = n.id;
      }
    }
    return best;
  }

  /** The link whose drawn curve passes closest to a point on screen. */
  pickWire(x: number, y: number, project: (v: Vector3) => Vector3, width: number, height: number, radius = 9): string | null {
    let best: string | null = null;
    let nearest = radius * radius;
    for (const w of this.wires) {
      if (w.visible < 0.25) continue;
      for (let i = 1; i < 32; i++) {
        const s = project(pointOn(w, i / 32, HIT));
        if (s.z > 1) continue;
        const dx = ((s.x + 1) / 2) * width - x;
        const dy = ((1 - s.y) / 2) * height - y;
        const d = dx * dx + dy * dy;
        if (d < nearest) {
          nearest = d;
          best = w.id;
        }
      }
    }
    return best;
  }

  /** The space these components will occupy when the animation has arrived. */
  targetBox(ids: Iterable<string>, out = new Box3()): Box3 {
    out.makeEmpty();
    for (const id of ids) {
      const n = this.nodes.get(id);
      if (!n) continue;
      out.expandByPoint(HIT.set(n.toX - n.size.w / 2, 0, n.toZ - n.size.d / 2));
      out.expandByPoint(HIT.set(n.toX + n.size.w / 2, n.size.h + 1.1, n.toZ + n.size.d / 2));
    }
    return out;
  }
}

const BOX = new Box3();
const HIT = new Vector3();

export function boxOf(n: Node, out: Box3): Box3 {
  out.min.set(n.x - n.size.w / 2, 0, n.z - n.size.d / 2);
  out.max.set(n.x + n.size.w / 2, n.size.h, n.z + n.size.d / 2);
  return out;
}

function loadOf(c: TwinComponent): number {
  return c.display.load ?? -1;
}

/** Requests travel in arcs above the racks, cables run along the floor. */
function curve(w: Wire): void {
  const { source: a, target: b, p } = w;
  const dx = b.x - a.x;
  const dz = b.z - a.z;
  const dist = Math.hypot(dx, dz) || 1;
  const ux = dx / dist;
  const uz = dz / dist;
  if (FLOW_KINDS.has(w.kind)) {
    const ya = a.size.h + 0.04;
    const yb = b.size.h + 0.04;
    const lift = 1.1 + dist * 0.16;
    const lean = dist * 0.18;
    p.set([
      a.x + ux * a.size.w * 0.18, ya, a.z + uz * a.size.d * 0.18,
      a.x + ux * lean, ya + lift, a.z + uz * lean,
      b.x - ux * lean, yb + lift, b.z - uz * lean,
      b.x - ux * b.size.w * 0.18, yb, b.z - uz * b.size.d * 0.18,
    ]);
  } else {
    const y = 0.07 + 0.03 * (LINK_CODE[w.kind] - 3);
    const side = dist * 0.16;
    p.set([
      a.x + ux * a.size.w * 0.45, y, a.z + uz * a.size.d * 0.45,
      a.x + dx * 0.33 - uz * side, y, a.z + dz * 0.33 + ux * side,
      a.x + dx * 0.67 - uz * side, y, a.z + dz * 0.67 + ux * side,
      b.x - ux * b.size.w * 0.45, y, b.z - uz * b.size.d * 0.45,
    ]);
  }
}

export function pointOn(w: Wire, t: number, out: Vector3): Vector3 {
  const u = 1 - t;
  const a = u * u * u;
  const b = 3 * u * u * t;
  const c = 3 * u * t * t;
  const d = t * t * t;
  const p = w.p;
  return out.set(
    a * p[0] + b * p[3] + c * p[6] + d * p[9],
    a * p[1] + b * p[4] + c * p[7] + d * p[10],
    a * p[2] + b * p[5] + c * p[8] + d * p[11],
  );
}

export const world = new World();
