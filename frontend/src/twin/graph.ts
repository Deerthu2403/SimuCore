import type { TwinComponent, TwinLink } from "./types";

type Links = Readonly<Record<string, TwinLink>>;

export interface Reach {
  /** component -> steps. Negative: it depends on the centre. Positive: the centre depends on it. */
  steps: Map<string, number>;
  links: Set<string>;
}

/** Links that mean "needs". Being plugged into the network or observed is not a dependency one
 *  would follow across the whole bank. */
const FOLLOWED = new Set(["calls", "routes_to", "stores_in", "hosted_on"]);

function adjacency(links: Links): { out: Map<string, TwinLink[]>; in: Map<string, TwinLink[]> } {
  const out = new Map<string, TwinLink[]>();
  const into = new Map<string, TwinLink[]>();
  for (const id in links) {
    const k = links[id];
    (out.get(k.source) ?? out.set(k.source, []).get(k.source)!).push(k);
    (into.get(k.target) ?? into.set(k.target, []).get(k.target)!).push(k);
  }
  return { out, in: into };
}

/** Everything the centre depends on and everything that depends on it, with the fewest steps. */
export function reach(center: string, links: Links, kinds: ReadonlySet<string> = FOLLOWED): Reach {
  const adj = adjacency(links);
  const steps = new Map<string, number>([[center, 0]]);
  const used = new Set<string>();
  const walk = (side: "out" | "in", sign: 1 | -1) => {
    const seen = new Set([center]);
    let front = [center];
    for (let n = 1; front.length; n++) {
      const next: string[] = [];
      for (const id of front) {
        for (const k of adj[side].get(id) ?? []) {
          if (!kinds.has(k.kind)) continue;
          used.add(k.id);
          const other = side === "out" ? k.target : k.source;
          if (seen.has(other)) continue;
          seen.add(other);
          next.push(other);
          if (!steps.has(other)) steps.set(other, sign * n);
        }
      }
      front = next;
    }
  };
  walk("out", 1);
  walk("in", -1);
  return { steps, links: used };
}

/** Rows for drawing a reach so that every link points downwards: the longest path from the centre
 *  (+, what it depends on) or to it (-, what depends on it). */
export function rows(center: string, r: Reach, links: Links): Map<string, number> {
  const inside = Object.fromEntries([...r.links].flatMap((id) => (links[id] ? [[id, links[id]]] : [])));
  const below = [...r.steps].filter(([, s]) => s >= 0).map(([id]) => id);
  const above = [...r.steps].filter(([, s]) => s <= 0).map(([id]) => id);
  const down = depths(below, inside, FOLLOWED);
  const reversed = Object.fromEntries(Object.values(inside).map((k) => [k.id, { ...k, source: k.target, target: k.source }]));
  const up = depths(above, reversed, FOLLOWED);
  return new Map([...above.map((id): [string, number] => [id, id === center ? 0 : -up.get(id)!]), ...below.map((id): [string, number] => [id, down.get(id)!])]);
}

/** The component and everything directly connected to it. */
export function neighbours(center: string, links: Links): Reach {
  const steps = new Map<string, number>([[center, 0]]);
  const used = new Set<string>();
  for (const id in links) {
    const k = links[id];
    if (k.source !== center && k.target !== center) continue;
    used.add(id);
    if (k.source === center && !steps.has(k.target)) steps.set(k.target, 1);
    if (k.target === center && !steps.has(k.source)) steps.set(k.source, -1);
  }
  return { steps, links: used };
}

/** Longest path from an entry point over the given kinds: who is called by whom. */
export function depths(ids: readonly string[], links: Links, kinds: ReadonlySet<string>): Map<string, number> {
  const { out } = adjacency(links);
  const depth = new Map<string, number>(ids.map((i) => [i, 0]));
  const state = new Map<string, 1 | 2>();
  const order: string[] = [];
  const visit = (id: string) => {
    if (state.has(id)) return; // also breaks cycles
    state.set(id, 1);
    for (const k of out.get(id) ?? []) if (kinds.has(k.kind) && depth.has(k.target)) visit(k.target);
    state.set(id, 2);
    order.push(id);
  };
  ids.forEach(visit);
  for (const id of order.reverse()) {
    for (const k of out.get(id) ?? []) {
      if (kinds.has(k.kind) && depth.has(k.target) && k.target !== id) {
        depth.set(k.target, Math.max(depth.get(k.target)!, depth.get(id)! + 1));
      }
    }
  }
  return depth;
}

/** Search by name, id, type, status, description or attribute. Every word must match. */
export function search(query: string, components: readonly TwinComponent[]): TwinComponent[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [...components];
  const scored: { c: TwinComponent; score: number }[] = [];
  for (const c of components) {
    const name = c.name.toLowerCase();
    const id = c.id.toLowerCase();
    const rest = [
      c.type.replace("_", " "), c.status, c.description, c.display.layer, c.display.group,
      ...Object.values(c.attributes).map(String),
    ].join(" ").toLowerCase();
    let score = 0;
    const all = words.every((w) => {
      const s = name.startsWith(w) ? 5
        : name.split(/\s+/).some((p) => p.startsWith(w)) ? 4
        : id.includes(w) ? 3
        : name.includes(w) ? 2
        : rest.includes(w) ? 1 : 0;
      score += s;
      return s > 0;
    });
    if (all) scored.push({ c, score });
  }
  return scored.sort((a, b) => b.score - a.score || a.c.name.localeCompare(b.c.name)).map((x) => x.c);
}
