import { depths, type Reach } from "../twin/graph";
import type { ComponentType, TwinComponent, TwinLink } from "../twin/types";

/** Where things stand on the floor. Derived from the structure the twin reports, never from a list
 *  of known names. Traffic runs left to right: edge, services, host, storage, platform. */
export interface Place {
  x: number;
  z: number;
}

export interface Zone {
  id: string;
  label: string;
  x0: number;
  z0: number;
  x1: number;
  z1: number;
}

export interface Layout {
  places: Map<string, Place>;
  zones: Zone[];
  bounds: { x0: number; z0: number; x1: number; z1: number };
}

export interface Size {
  w: number;
  h: number;
  d: number;
}

/** Outer dimensions in metres (before the criticality scale). */
export const SIZE: Record<ComponentType, Size> = {
  microservice: { w: 1.5, h: 2.7, d: 1.4 },
  database: { w: 2.1, h: 2.3, d: 2.1 },
  server: { w: 2.3, h: 2.4, d: 1.6 },
  network: { w: 3.6, h: 1.25, d: 1.5 },
  load_balancer: { w: 1.9, h: 3.1, d: 1.9 },
  gateway: { w: 2.6, h: 2.9, d: 1.1 },
  monitoring: { w: 2.4, h: 1.75, d: 1.4 },
};

export const scaleOf = (c: Pick<TwinComponent, "display">) => 0.85 + 0.03 * c.display.size;

const DX = 3.3;
const DZ = 4.8;

export function computeLayout(
  ids: readonly string[],
  components: Readonly<Record<string, TwinComponent>>,
  links: Readonly<Record<string, TwinLink>>,
): Layout {
  const places = new Map<string, Place>();
  const of = (...types: ComponentType[]) => ids.filter((i) => types.includes(components[i].type));
  const depth = depths(ids, links, new Set(["calls", "routes_to"]));
  const hostOf = new Map<string, string>();
  for (const id in links) {
    const k = links[id];
    if (k.kind === "hosted_on") hostOf.set(k.source, k.target);
  }

  // --- services, in one block of cabinet rows per host
  const services = of("microservice").sort(
    (a, b) => depth.get(a)! - depth.get(b)! || components[a].display.order - components[b].display.order);
  const blocks = new Map<string, string[]>();
  for (const id of services) {
    const h = hostOf.get(id) ?? "";
    (blocks.get(h) ?? blocks.set(h, []).get(h)!).push(id);
  }
  const serving = [...blocks.keys()].sort();
  const shape = (n: number): [rows: number, cols: number] => {
    const rows = Math.max(1, Math.round(Math.sqrt(n / 2.2)));
    return [rows, Math.ceil(n / rows)];
  };
  // Several hosts: side by side in shelves, so that the hall stays wider than deep.
  const blockW = Math.max(...serving.map((h) => shape(blocks.get(h)!.length)[1]), 1) * DX + 5.4;
  const blockD = Math.max(...serving.map((h) => shape(blocks.get(h)!.length)[0]), 1) * DZ + 1.2;
  const perShelf = Math.max(1, Math.round(Math.sqrt((serving.length * 1.7 * blockD) / blockW)));
  let width = 0;
  const hostPlace = new Map<string, Place>();
  serving.forEach((h, n) => {
    const list = blocks.get(h)!;
    const [rows, cols] = shape(list.length);
    const x0 = (n % perShelf) * blockW;
    const z0 = Math.floor(n / perShelf) * blockD;
    list.forEach((id, i) => places.set(id, { x: x0 + Math.floor(i / rows) * DX, z: z0 + (i % rows) * DZ }));
    width = Math.max(width, x0 + (cols - 1) * DX);
    hostPlace.set(h, { x: x0 + cols * DX + 0.3, z: z0 + ((rows - 1) * DZ) / 2 });
  });
  const depthZ = Math.max(0, Math.ceil(serving.length / perShelf) * blockD - DZ - 1.2);
  const midZ = depthZ / 2;

  const column = (list: string[], x: number, gap: number): number => {
    const perColumn = Math.max(2, Math.floor(depthZ / gap) + 1);
    const cols = Math.ceil(list.length / perColumn);
    list.forEach((id, i) => {
      const col = Math.floor(i / perColumn);
      const inCol = Math.min(perColumn, list.length - col * perColumn);
      places.set(id, { x: x + col * gap * 0.9, z: midZ + ((i % perColumn) - (inCol - 1) / 2) * gap });
    });
    return Math.max(0, cols - 1) * gap * 0.9;
  };

  // --- where traffic enters and leaves: the left edge
  const edge = of("load_balancer", "gateway").sort(
    (a, b) => depth.get(b)! - depth.get(a)! || components[a].display.order - components[b].display.order);
  const edgeCols = column(edge, 0, 5);
  for (const id of edge) places.get(id)!.x -= 4.7 + edgeCols;

  // --- the hosts of the services, then the bank's storage, then the platform
  let x = width;
  for (const [h, p] of hostPlace) {
    if (!h || !components[h]) continue;
    places.set(h, p);
    x = Math.max(x, p.x);
  }
  const stores = of("database").filter((i) => components[i].display.group === "bank");
  x += 4;
  x += column(stores, x, 4.2);
  const placed = (i: string) => places.has(i);
  // --- along the back wall: the network core and the platform that observes the bank
  const net = of("network");
  const netCols = Math.max(1, Math.floor(width / 2 / 5) + 1);
  const netRows = Math.ceil(net.length / netCols);
  let back = -5.6;
  net.forEach((id, i) => places.set(id, { x: 1.2 + (i % netCols) * 5, z: back - Math.floor(i / netCols) * 4.6 }));
  const netRight = net.length ? 1.2 + (Math.min(net.length, netCols) - 1) * 5 + 2.3 : -2;
  const rest = ids.filter((i) => !placed(i)).sort(
    (a, b) => ["server", "database", "monitoring"].indexOf(components[a].type)
      - ["server", "database", "monitoring"].indexOf(components[b].type) || components[a].display.order - components[b].display.order);
  let perRow = Math.floor((x - netRight - 1.4) / 3.9) + 1;
  if (perRow < Math.min(4, rest.length)) { // no room beside the network: behind it
    back -= netRows * 4.6;
    perRow = Math.max(4, Math.floor(x / 3.9));
  }
  const rows = Math.ceil(rest.length / perRow);
  rest.forEach((id, i) => {
    const inRow = Math.min(perRow, rest.length - Math.floor(i / perRow) * perRow);
    places.set(id, { x: x - (inRow - 1 - (i % perRow)) * 3.9, z: back - (rows - 1 - Math.floor(i / perRow)) * 4.6 });
  });

  // --- centre on the origin
  const box = boundsOf(places.values());
  const cx = (box.x0 + box.x1) / 2;
  const cz = (box.z0 + box.z1) / 2;
  for (const p of places.values()) {
    p.x -= cx;
    p.z -= cz;
  }

  // --- floor markings
  const zones: Zone[] = [];
  const zone = (id: string, label: string, members: string[], pad = 1.9) => {
    const list = members.filter(placed);
    if (!list.length) return;
    const b = boundsOf(list.map((i) => places.get(i)!));
    zones.push({ id, label, x0: b.x0 - pad, x1: b.x1 + pad, z0: b.z0 - pad, z1: b.z1 + pad });
  };
  for (const h of serving) {
    zone(`host:${h}`, components[h] ? `Racks on ${components[h].name}` : "Service racks",
      [...blocks.get(h)!, ...(components[h] ? [h] : [])]);
  }
  zone("edge", "Perimeter", edge, 2.2);
  zone("data", "Bank storage", stores, 1.9);
  zone("platform", "Platform and monitoring", rest, 2);
  zone("network", "Network core", net, 2.2);
  return { places, zones, bounds: boundsOf(places.values()) };
}

function boundsOf(places: Iterable<Place>): Layout["bounds"] {
  const b = { x0: Infinity, z0: Infinity, x1: -Infinity, z1: -Infinity };
  for (const p of places) {
    b.x0 = Math.min(b.x0, p.x);
    b.x1 = Math.max(b.x1, p.x);
    b.z0 = Math.min(b.z0, p.z);
    b.z1 = Math.max(b.z1, p.z);
  }
  return Number.isFinite(b.x0) ? b : { x0: 0, z0: 0, x1: 0, z1: 0 };
}

/** The dependency view: the centre in the middle, what depends on it to the left, what it depends
 *  on to the right, one column per step. */
export function dependencyLayout(center: string, r: Reach, base: Layout): Map<string, Place> {
  const columns = new Map<number, string[]>();
  for (const [id, s] of r.steps) (columns.get(s) ?? columns.set(s, []).get(s)!).push(id);
  const out = new Map<string, Place>();
  const c = base.places.get(center) ?? { x: 0, z: 0 };
  const perColumn = 5;
  const lanes = (side: 1 | -1) => {
    let x = 0;
    for (let s = 1; columns.has(s * side); s++) {
      const list = columns.get(s * side)!.sort(
        (a, b) => (base.places.get(a)?.z ?? 0) - (base.places.get(b)?.z ?? 0) || (a < b ? -1 : 1));
      const sub = Math.ceil(list.length / perColumn);
      x += 5.6;
      list.forEach((id, i) => {
        const col = Math.floor(i / perColumn);
        const n = Math.min(perColumn, list.length - col * perColumn);
        out.set(id, { x: c.x + side * (x + col * 3.6), z: c.z + ((i % perColumn) - (n - 1) / 2) * 4.2 });
      });
      x += (sub - 1) * 3.6;
    }
  };
  out.set(center, { ...c });
  lanes(1);
  lanes(-1);
  return out;
}
