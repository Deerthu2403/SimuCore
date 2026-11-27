import { readFileSync } from "node:fs";
import { Box3, Ray, Vector3 } from "three";
import { describe, expect, it } from "vitest";
import { archetype, PART } from "../scene/archetypes";
import { fitDistance } from "../scene/CameraRig";
import { computeLayout, dependencyLayout, SIZE } from "../scene/layout";
import { pointOn, World, type ViewInput } from "../scene/world";
import { STATUS } from "../twin/format";
import { reach } from "../twin/graph";
import { ICON_NAMES, iconOf, ICONS } from "../twin/icons";
import { apply, type Mirror } from "../twin/mirror";
import { COMPONENT_TYPES, LINK_KINDS, STATUSES, type TwinComponent, type TwinLink } from "../twin/types";
import { bank, byId, component, link, message } from "./fixtures";

const mirror = (cs: TwinComponent[], ks: TwinLink[], prev: Mirror | null = null, v = 1): Mirror => {
  const r = apply(prev, message("snapshot", v, cs, ks));
  if (!r.ok) throw new Error(r.why);
  return r.mirror;
};
const lay = (cs: TwinComponent[], ks: TwinLink[]) => computeLayout(cs.map((c) => c.id), byId(cs), byId(ks));

/** Many components of every type, as large installations have. */
function many(n: number) {
  const cs = Array.from({ length: n }, (_, i) => component(`c${i}`, { type: COMPONENT_TYPES[i % 10 < 6 ? 0 : i % 10 - 5] ?? "microservice" }));
  const ks = cs.slice(1).map((c, i) => link(cs[Math.floor(i / 3)].id, c.id));
  return { cs, ks };
}

describe("layout", () => {
  it.each([1, 3, 17, 300, 1000])("places %s components without overlap", (n) => {
    const { cs, ks } = many(n);
    const { places } = lay(cs, ks);
    expect(places.size).toBe(n);
    const boxes = cs.map((c) => ({ ...places.get(c.id)!, ...SIZE[c.type] }));
    for (const b of boxes) expect(Number.isFinite(b.x) && Number.isFinite(b.z)).toBe(true);
    for (let i = 0; i < boxes.length; i++) {
      for (let k = i + 1; k < boxes.length; k++) {
        const [a, b] = [boxes[i], boxes[k]];
        const apart = Math.abs(a.x - b.x) >= (a.w + b.w) / 2 || Math.abs(a.z - b.z) >= (a.d + b.d) / 2;
        if (!apart) throw new Error(`${cs[i].id} (${cs[i].type}) and ${cs[k].id} (${cs[k].type}) overlap`);
      }
    }
  });
  it("stays wider than deep with many hosts", () => {
    const cs = Array.from({ length: 60 }, (_, h) => [component(`h${h}`, { type: "server" }), ...Array.from({ length: 8 }, (_, i) => component(`s${h}-${i}`))]).flat();
    const ks = cs.filter((c) => c.type === "microservice").map((c) => link(c.id, `h${c.id.slice(1).split("-")[0]}`, "hosted_on"));
    const { bounds: b, zones } = lay(cs, ks);
    const ratio = (b.x1 - b.x0) / (b.z1 - b.z0);
    expect(ratio).toBeGreaterThan(0.9);
    expect(ratio).toBeLessThan(4);
    for (const p of zones) for (const q of zones) {
      if (p !== q && p.x0 < q.x1 && q.x0 < p.x1 && p.z0 < q.z1 && q.z0 < p.z1) throw new Error(`${p.id} and ${q.id} overlap`);
    }
  });
  it("is the same every time, and does not depend on values", () => {
    const b = bank();
    const first = lay(b.components, b.links);
    b.components.forEach((c) => {
      c.status = "failed";
      c.metrics = { cpu_pct: 99 };
    });
    expect(lay(b.components, b.links).places).toEqual(first.places);
  });
  it("runs from the edge over the services to the storage", () => {
    const b = bank();
    const { places, zones } = lay(b.components, b.links);
    const x = (id: string) => places.get(id)!.x;
    expect(x("entry")).toBeLessThan(x("a"));
    expect(x("dbs")).toBeLessThan(x("store"));
    expect(zones.map((z) => z.label)).toContain("Racks on host Service");
  });
  it("marks one zone per host", () => {
    const b = bank();
    b.components.push(component("host2", { type: "server" }));
    b.links = b.links.filter((k) => k.id !== "b->host");
    b.links.push(link("b", "host2", "hosted_on"));
    const z = lay(b.components, b.links).zones.filter((x) => x.id.startsWith("host:"));
    expect(z.map((x) => x.id).sort()).toEqual(["host:host", "host:host2"]);
    const overlap = !(z[0].z1 < z[1].z0 || z[1].z1 < z[0].z0);
    expect(overlap).toBe(false);
  });
  it("spreads dependencies to the right and dependents to the left", () => {
    const b = bank();
    const base = lay(b.components, b.links);
    const p = dependencyLayout("b", reach("b", byId(b.links)), base);
    expect(p.get("a")!.x).toBeLessThan(p.get("b")!.x);
    expect(p.get("entry")!.x).toBeLessThan(p.get("a")!.x);
    expect(p.get("store")!.x).toBeGreaterThan(p.get("dbs")!.x);
    expect(p.has("net")).toBe(false);
    const spots = [...p.values()].map((q) => `${q.x.toFixed(2)},${q.z.toFixed(2)}`);
    expect(new Set(spots).size).toBe(spots.length);
  });
});

describe("world", () => {
  const view: ViewInput = {
    reach: null, mode: "normal", selected: null, selectedLink: null, hovered: null, hiddenTypes: new Set(), statusFilter: null, structural: false,
  };
  const fresh = () => {
    const b = bank();
    const w = new World();
    w.sync(mirror(b.components, b.links));
    w.view(view);
    return { w, ...b };
  };
  const run = (w: World, seconds: number, motion = true) => {
    for (let t = 0; t < seconds; t += 1 / 60) w.step(1 / 60, motion);
  };

  it("shows a failure at once and its colour within half a second", () => {
    const { w, components, links } = fresh();
    run(w, 1);
    const n = w.nodes.get("a")!;
    expect(`#${n.color.getHexString()}`).toBe(STATUS.healthy.color);
    components[1] = { ...components[1], status: "failed" };
    w.sync(mirror(components, links));
    expect(n.status).toBe("failed"); // signs, waves and lights follow the status itself
    expect(n.flashAt).toBe(w.clock);
    run(w, 0.5);
    const [r, g] = [n.color.r, n.color.g];
    expect(r).toBeGreaterThan(0.75 * 0.87);
    expect(g).toBeLessThan(0.15);
    run(w, 2);
    expect(`#${n.color.getHexString()}`).toBe(STATUS.failed.color);
  });
  it("follows load and traffic; unknown load is not zero load", () => {
    const { w, components, links } = fresh();
    run(w, 1);
    expect(w.nodes.get("a")!.load).toBe(-1);
    components[1] = { ...components[1], display: { ...components[1].display, load: 0.8 } };
    links[1] = { ...links[1], display: { flow: 0.5, attention: false }, metrics: { failure_rate: 0.25 } };
    w.sync(mirror(components, links));
    run(w, 4);
    expect(w.nodes.get("a")!.load).toBeCloseTo(0.8, 2);
    const k = w.wires.find((x) => x.id === "a->b")!;
    expect([k.flow, k.failing]).toEqual([0.5, 0.25]);
  });
  it("moves requests faster with more traffic, never in jumps", () => {
    const { w, components, links } = fresh();
    const k = () => w.wires.find((x) => x.id === "a->b")!;
    const speed = () => {
      const before = k().phase;
      w.step(1 / 60, true);
      return (k().phase - before + 1) % 1;
    };
    run(w, 1);
    const slow = speed();
    links[1] = { ...links[1], display: { flow: 1, attention: false } };
    w.sync(mirror(components, links));
    const now = speed();
    expect(now).toBeLessThan(slow * 1.5); // at the moment of the change
    run(w, 5);
    expect(speed()).toBeGreaterThan(slow * 3);
  });
  it("isolates: the rest disappears, with its links, and comes back", () => {
    const { w, links } = fresh();
    w.view({ ...view, mode: "isolate", selected: "b", reach: { steps: new Map([["b", 0], ["a", -1]]), links: new Set(["a->b"]) } });
    run(w, 3);
    const visible = [...w.nodes.values()].filter((n) => n.visible > 0.5).map((n) => n.id).sort();
    expect(visible).toEqual(["a", "b"]);
    expect(w.wires.filter((x) => x.visible > 0).map((x) => x.id)).toEqual(["a->b"]);
    w.view(view);
    run(w, 3);
    expect([...w.nodes.values()].every((n) => n.visible === 1)).toBe(true);
    expect(w.wires.filter((x) => x.visible === 1).map((x) => x.kind)).not.toContain("hosted_on");
    expect(links.length).toBe(w.wires.length);
  });
  it("glides to the dependency view; links stay attached on the way", () => {
    const { w, links } = fresh();
    run(w, 1);
    const n = w.nodes.get("store")!;
    const from = n.x;
    w.view({ ...view, mode: "dependencies", selected: "b", reach: reach("b", byId(links)) });
    w.step(1 / 60, true);
    expect(Math.abs(n.x - from)).toBeLessThan(Math.abs(n.toX - from) * 0.3);
    // the link whose target has the longest way
    const way = (x: { target: { x: number; toX: number; z: number; toZ: number } }) => Math.hypot(x.target.toX - x.target.x, x.target.toZ - x.target.z);
    const k = [...w.wires].filter((x) => x.toVisible > 0).sort((p, q) => way(q) - way(p))[0];
    const [x0, z0] = [k.target.x, k.target.z];
    for (let i = 0; i < 30; i++) w.step(1 / 60, true);
    expect(Math.hypot(k.target.x - x0, k.target.z - z0)).toBeGreaterThan(2);
    const end = pointOn(k, 1, new Vector3());
    expect(Math.hypot(end.x - k.target.x, end.z - k.target.z)).toBeLessThan(1.2);
    run(w, 3);
    expect(n.x).toBe(n.toX);
    expect(w.settled).toBe(true);
  });
  it("with reduced motion everything arrives in the next frame and nothing travels", () => {
    const { w, links } = fresh();
    w.view({ ...view, mode: "dependencies", selected: "b", reach: reach("b", byId(links)) });
    const phase = w.wires[0].phase;
    w.step(1 / 60, false);
    for (const n of w.nodes.values()) expect([n.x, n.z, n.visible]).toEqual([n.toX, n.toZ, n.toVisible]);
    w.step(1 / 60, false);
    expect(w.settled).toBe(true);
    expect(w.wires[0].phase).toBe(phase);
  });
  it("hides types, dims what a status filter leaves out, reveals cables on demand", () => {
    const { w } = fresh();
    w.view({ ...view, hiddenTypes: new Set(["database"]), statusFilter: "failed" });
    expect(w.nodes.get("store")!.toVisible).toBe(0);
    expect(w.nodes.get("a")!.toVisible).toBeLessThan(0.5);
    expect(w.wires.find((k) => k.id === "dbs->store")!.toVisible).toBe(0);
    w.view({ ...view, structural: true });
    expect(w.wires.every((k) => k.toVisible === 1)).toBe(true);
    w.view({ ...view, selected: "mon" });
    expect(w.wires.find((k) => k.id === "mon->store")!.toVisible).toBe(1);
    expect(w.wires.find((k) => k.id === "a->b")!.toVisible).toBeLessThan(0.5);
  });
  it("adds and removes components without moving the others by a jump", () => {
    const { w, components, links } = fresh();
    run(w, 1);
    const before = w.nodes.get("a")!.x;
    w.sync(mirror([...components, component("new")], links));
    expect(w.nodes.get("a")!.x).toBe(before);
    expect(w.nodes.get("new")!.visible).toBe(0);
    run(w, 3);
    expect(w.nodes.get("new")!.visible).toBe(1);
    w.sync(mirror(components.filter((c) => c.id !== "b"), links.filter((k) => k.source !== "b" && k.target !== "b")));
    expect(w.nodes.has("b")).toBe(false);
  });
  it("picks the nearest visible component", () => {
    const { w } = fresh();
    run(w, 1);
    const a = w.nodes.get("a")!;
    const ray = new Ray(new Vector3(a.x, 1, a.z + 40), new Vector3(0, 0, -1));
    const hit = w.pick(ray)!;
    const n = w.nodes.get(hit)!;
    expect(Math.abs(n.x - a.x)).toBeLessThan(1.5);
    n.visible = 0;
    expect(w.pick(ray)).not.toBe(hit);
    expect(w.pick(new Ray(new Vector3(0, 50, 0), new Vector3(0, 1, 0)))).toBeNull();
  });
  it("picks a link close to the pointer", () => {
    const { w } = fresh();
    run(w, 2);
    const k = w.wires.find((x) => x.id === "dbs->store")!;
    const m = pointOn(k, 0.5, new Vector3());
    const project = (v: Vector3) => v.set(v.x / 20, (v.y - m.y) / 20, 0);
    const x = ((m.x / 20 + 1) / 2) * 1000;
    expect(w.pickWire(x, 500, project, 1000, 1000)).toBe("dbs->store");
    expect(w.pickWire(x, 10, project, 1000, 1000)).toBeNull();
  });
});

describe("camera", () => {
  it.each([[-0.3, 0.98, 1.4], [0, 0.02, 0.6], [0, 1.24, 2.4], [-1.5, 1.1, 1]])("fits a box seen from %s/%s (aspect %s)", (az, pol, aspect) => {
    const box = new Box3(new Vector3(-12, 0, -8), new Vector3(16, 3, 4));
    const { center, distance } = fitDistance(box, az, pol, 42, aspect);
    const back = new Vector3(Math.sin(pol) * Math.sin(az), Math.cos(pol), Math.sin(pol) * Math.cos(az));
    const right = new Vector3(Math.cos(az), 0, -Math.sin(az));
    const up = new Vector3().crossVectors(back, right);
    const tan = Math.tan((42 * Math.PI) / 360);
    let reach = 0;
    for (let i = 0; i < 8; i++) {
      const p = new Vector3(i & 1 ? 16 : -12, i & 2 ? 3 : 0, i & 4 ? 4 : -8).sub(center);
      const z = distance - p.dot(back);
      expect(z).toBeGreaterThan(0);
      const x = Math.abs(p.dot(right)) / (z * tan * aspect);
      const y = Math.abs(p.dot(up)) / (z * tan);
      reach = Math.max(reach, x, y);
    }
    expect(reach).toBeLessThanOrEqual(1.02); // everything inside the picture ...
    expect(reach).toBeGreaterThan(0.8); // ... and not lost in it
  });
});

describe("objects", () => {
  it.each(COMPONENT_TYPES)("%s has one geometry with lights and stays within its size", (type) => {
    const g = archetype(type);
    const parts = new Set(Array.from(g.getAttribute("aPart").array));
    expect(parts.has(PART.status)).toBe(true);
    g.computeBoundingBox();
    const s = g.boundingBox!.getSize(new Vector3());
    expect(s.x).toBeLessThan(SIZE[type].w * 1.12);
    expect(s.y).toBeLessThan(SIZE[type].h * 1.08);
    expect(s.z).toBeLessThan(SIZE[type].d * 1.12);
    expect(g.index!.count / 3).toBeLessThan(4000);
    for (const v of g.getAttribute("position").array) expect(Number.isFinite(v)).toBe(true);
  });
  it("each banking service and each type has its own symbol", () => {
    const services = ["authentication", "customer", "account", "transaction", "payment", "fraud", "notification", "database"];
    const icons = [
      ...services.map((service) => iconOf({ type: "microservice", attributes: { service } })),
      ...COMPONENT_TYPES.map((type) => iconOf({ type, attributes: {} })),
    ];
    expect(new Set(icons).size).toBe(icons.length);
    expect(iconOf({ type: "microservice", attributes: { service: "something new" } })).toBe("cube");
    expect(ICON_NAMES.length).toBeLessThanOrEqual(64); // cells of the texture
  });
  it("statuses differ in symbol and word, not only in colour", () => {
    for (const k of ["icon", "label", "color"] as const) expect(new Set(STATUSES.map((s) => STATUS[s][k])).size).toBe(STATUSES.length);
    for (const s of STATUSES) expect(STATUS[s].icon in ICONS).toBe(true);
  });
});

describe("contract", () => {
  const schema = JSON.parse(readFileSync(new URL("../../../docs/api/twin-v2.schema.json", import.meta.url), "utf-8"));
  const source = readFileSync(new URL("../twin/types.ts", import.meta.url), "utf-8");
  const defs = { ...schema.TwinView.$defs, ...schema.TwinMessage.$defs };
  const fields = (name: string) => {
    const body = new RegExp(`export interface ${name} \\{([\\s\\S]*?)\\n\\}`).exec(source)![1];
    return [...body.matchAll(/^ {2}(\w+)\??:/gm)].map((m) => m[1]).sort();
  };
  const published = (name: string) => Object.keys(defs[name].properties).sort();

  it.each([
    ["TwinComponent", "ComponentView"], ["TwinLink", "LinkView"], ["Summary", "Summary"], ["Meta", "TwinMeta"],
    ["Finding", "FindingView"], ["DependencyRef", "DependencyRef"], ["Insight", "InsightView"],
    ["AnomalyOpinion", "AnomalyView"], ["RiskOpinion", "RiskView"], ["PredictiveWarning", "PredictiveWarning"],
  ])("%s has the fields of %s", (ours, theirs) => expect(fields(ours)).toEqual(published(theirs)));
  it("transitions have the published fields", () => expect(fields("Transition").filter((f) => f !== "run_id")).toEqual(published("TransitionView")));
  it("knows every status, type and kind of link", () => {
    expect([...STATUSES].sort()).toEqual([...defs.Status.enum].sort());
    expect([...COMPONENT_TYPES].sort()).toEqual([...defs.ComponentType.enum].sort());
    expect([...LINK_KINDS].sort()).toEqual([...defs.LinkKind.enum].sort());
  });
});

describe("no invented data", () => {
  const files = ["scene/TwinScene.tsx", "scene/world.ts", "scene/objects.ts", "scene/layout.ts", "scene/Labels.tsx", "ui/Inspector.tsx", "ui/LeftPanel.tsx", "ui/Events.tsx", "ui/Chrome.tsx", "ui/DependencyGraph.tsx", "ui/Ai.tsx", "twin/ai.ts", "twin/store.ts", "twin/samples.ts", "twin/view.ts"];
  it.each(files)("%s contains no random values and no component ids", (f) => {
    const s = readFileSync(new URL(`../${f}`, import.meta.url), "utf-8");
    expect(s).not.toMatch(/Math\.random/);
    expect(s).not.toMatch(/["'`](bank-[a-z]+|store-[a-z]+|server-[a-z]+|net-bank|lb-clients|gw-external|mon-[a-z]+)["'`]/);
  });
});
