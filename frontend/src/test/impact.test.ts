import { readFileSync } from "node:fs";
import { describe as group, expect, it } from "vitest";
import { World, type ViewInput } from "../scene/world";
import { STATUS } from "../twin/format";
import { AI_COLOR } from "../twin/ai";
import {
  describe, IMPACT_COLOR, impactOf, propagationOf, simulatedImpact, type Simulation,
} from "../twin/impact";
import { apply, type Mirror } from "../twin/mirror";
import type { ImpactView, TwinComponent, TwinLink } from "../twin/types";
import { bank, component, message } from "./fixtures";

const mirror = (cs: TwinComponent[], ks: TwinLink[]): Mirror => {
  const r = apply(null, message("snapshot", 1, cs, ks));
  if (!r.ok) throw new Error(r.why);
  return r.mirror;
};
const view: ViewInput = {
  reach: null, mode: "normal", selected: null, selectedLink: null, hovered: null, hiddenTypes: new Set(), statusFilter: null, structural: false,
};
const run = (w: World, seconds: number) => {
  for (let t = 0; t < seconds; t += 1 / 60) w.step(1 / 60, true);
};
const impact = (over: Partial<ImpactView>): ImpactView => ({
  role: "predicted", impact: 0.6, level: "high", score: 1.8, steps: 1, chain: ["dbs", "b"], links: ["b->dbs"], origins: ["dbs"], ...over,
});

/** dbs has failed: b depends on it (predicted), a on both (confirmed). */
function failing() {
  const { components, links } = bank();
  const at = (id: string) => components.findIndex((c) => c.id === id);
  components[at("dbs")] = { ...components[at("dbs")], status: "failed", impact: impact({ role: "origin", impact: 1, level: "failed", steps: 0, chain: ["dbs"], links: [] }) };
  components[at("b")] = { ...components[at("b")], impact: impact({}) };
  components[at("a")] = { ...components[at("a")], status: "warning", impact: impact({ role: "confirmed", impact: 0.9, level: "severe", chain: ["dbs", "a"], links: ["a->dbs"] }) };
  const ln = (id: string) => links.findIndex((k) => k.id === id);
  links[ln("b->dbs")] = { ...links[ln("b->dbs")], display: { ...links[ln("b->dbs")].display, propagation: 0.6, propagation_step: 1 } };
  links[ln("a->dbs")] = { ...links[ln("a->dbs")], display: { ...links[ln("a->dbs")].display, propagation: 0.9, propagation_step: 1 } };
  return { components, links };
}

const sim: Simulation = {
  what_if: true, title: "What if b failed", origins: [{ id: "b", name: "b Service", criticality: 3, status: "healthy" }],
  graph: "x", components: 8, dependencies: 11, reachable: 2, affected: 2, affected_severe: 1, affected_any: 2,
  expected_affected: 1.5, business_impact: 4.5, level: "severe",
  impacts: [
    { id: "a", name: "a Service", type: "microservice", impact: 1, level: "severe", meaning: "", criticality: 3, score: 3, steps: 1, chain: ["b Service", "a Service"], chain_ids: ["b", "a"], origins: ["b"], status: "healthy", confirmed: false },
    { id: "entry", name: "entry Service", type: "load_balancer", impact: 0.5, level: "high", meaning: "", criticality: 3, score: 1.5, steps: 2, chain: ["b Service", "a Service", "entry Service"], chain_ids: ["b", "a", "entry"], origins: ["b"], status: "healthy", confirmed: false },
  ],
  not_affected_dependencies: [{ id: "dbs", name: "dbs Service", kind: "calls", loses_calls_per_s: 3, of: "b" }],
  method: "",
  links: { "a->b": { impact: 1, step: 1 }, "entry->a": { impact: 0.5, step: 2 } },
};

group("propagation in the scene", () => {
  it("marks the origin, confirmed and predicted impacts differently", () => {
    const { components, links } = failing();
    const w = new World();
    w.sync(mirror(components, links));
    w.view(view);
    run(w, 2);
    const role = (id: string) => w.nodes.get(id)!.role;
    expect([role("dbs"), role("a"), role("b"), role("entry")]).toEqual([3, 2, 1, 0]);
    expect(w.nodes.get("b")!.impact).toBeCloseTo(0.6, 2);
    expect(w.nodes.get("entry")!.impact).toBe(0);
  });
  it("animates the path along the links, step by step", () => {
    const { components, links } = failing();
    const w = new World();
    w.sync(mirror(components, links));
    w.view(view);
    run(w, 2);
    const k = (id: string) => w.wires.find((x) => x.id === id)!;
    expect(k("b->dbs").propagation).toBeCloseTo(0.6, 2);
    expect(k("b->dbs").step).toBe(1);
    expect(k("a->b").propagation).toBe(0);
  });
  it("shows a cable when a failure travels along it, otherwise not", () => {
    const { components, links } = bank();
    const ln = links.findIndex((k) => k.id === "a->host");
    const w = new World();
    w.sync(mirror(components, links));
    w.view(view);
    run(w, 2);
    expect(w.wires.find((x) => x.id === "a->host")!.visible).toBe(0);
    links[ln] = { ...links[ln], display: { ...links[ln].display, propagation: 1, propagation_step: 1 } };
    w.sync(mirror(components, links));
    w.view(view); // as the scene does after every message
    run(w, 2);
    expect(w.wires.find((x) => x.id === "a->host")!.visible).toBe(1);
  });
  it("shows a simulation instead of the live estimate, and goes back", () => {
    const { components, links } = failing();
    const m = mirror(components, links);
    const w = new World();
    w.sync(m, sim);
    w.view(view);
    run(w, 2);
    expect(w.nodes.get("b")!.role).toBe(3); // simulated origin
    expect(w.nodes.get("dbs")!.role).toBe(0); // its real failure is not part of the simulation
    expect(w.nodes.get("entry")!.impact).toBeCloseTo(0.5, 2);
    expect(w.wires.find((x) => x.id === "entry->a")!.step).toBe(2);
    w.sync(m, null);
    run(w, 2);
    expect(w.nodes.get("dbs")!.role).toBe(3);
    expect(w.nodes.get("entry")!.role).toBe(0);
  });
  it("disappears when the failure is over", () => {
    const f = failing();
    const w = new World();
    w.sync(mirror(f.components, f.links));
    w.view(view);
    run(w, 1);
    const ok = bank();
    w.sync(mirror(ok.components, ok.links));
    run(w, 3);
    expect([...w.nodes.values()].every((n) => n.role === 0 && n.impact === 0)).toBe(true);
    expect(w.wires.every((k) => k.propagation === 0)).toBe(true);
  });
});

group("impact helpers", () => {
  it("turns a simulation into the twin's shape, with the links along the chain", () => {
    expect(simulatedImpact(sim, "entry")).toEqual({
      role: "predicted", impact: 0.5, level: "high", score: 1.5, steps: 2, chain: ["b", "a", "entry"],
      links: ["a->b", "entry->a"], origins: ["b"],
    });
    expect(simulatedImpact(sim, "b")!.role).toBe("origin");
    expect(simulatedImpact(sim, "dbs")).toBeNull();
  });
  it("uses the twin's own estimate without a simulation", () => {
    const c = component("x", { impact: impact({}) });
    expect(impactOf(c, null)).toBe(c.impact);
    expect(impactOf(c, sim)).toBeNull();
    const k = failing().links.find((l) => l.id === "b->dbs")!;
    expect(propagationOf(k, null)).toEqual({ impact: 0.6, step: 1 });
    expect(propagationOf(k, sim)).toEqual({ impact: 0, step: null });
  });
  it("describes an impact in words", () => {
    const name = (id: string) => ({ dbs: "Database Service", b: "B" }[id] ?? id);
    expect(describe(impact({}), name)).toBe("high 60 %, 1 step from Database");
    expect(describe(impact({ steps: 3, level: "low", impact: 0.07 }), name)).toBe("low 7 %, 3 steps from Database");
    expect(describe(impact({ role: "origin" }), name)).toBe("failure origin");
  });
  it("has a colour of its own, apart from statuses and the AI", () => {
    const taken = [...Object.values(STATUS).map((s) => s.color), ...Object.values(AI_COLOR)];
    expect(taken.map((c) => c.toLowerCase())).not.toContain(IMPACT_COLOR.toLowerCase());
  });
  it("the panel names no component and invents no numbers", () => {
    for (const f of ["../ui/Impact.tsx", "../twin/impact.ts"]) {
      const s = readFileSync(new URL(f, import.meta.url), "utf-8");
      expect(s).not.toMatch(/Math\.random/);
      expect(s).not.toMatch(/["'`]bank-[a-z]+["'`]/);
    }
  });
});
