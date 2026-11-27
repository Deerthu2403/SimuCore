import { beforeEach, describe, expect, it, vi } from "vitest";
import { World, type ViewInput } from "../scene/world";
import { AI_COLOR, isAnomalous, percent, riskOf, topRisk, topWarning } from "../twin/ai";
import { STATUS } from "../twin/format";
import { apply } from "../twin/mirror";
import * as samples from "../twin/samples";
import { useTwin } from "../twin/store";
import type { Insight, TwinMessage } from "../twin/types";
import { bank, component, message } from "./fixtures";

const opinion = (over: Partial<Insight> = {}): Insight => ({
  assessed_at: "2026-01-01T00:00:00Z", model: "m", anomaly_reason: "", risk_reason: "", warnings: [], reasons: [],
  anomaly: { score: 0.1, level: "normal", condition: "normal", since: null, contributions: [] },
  risks: [
    { target: "degradation", probability: 0.05, level: "low", horizon_s: 120 },
    { target: "failure", probability: 0.02, level: "low", horizon_s: 120 },
  ],
  ...over,
});
const anomaly = (score: number, level: "unusual" | "abnormal" = "abnormal") =>
  opinion({ anomaly: { score, level, condition: "resources", since: "2026-01-01T00:00:00Z", contributions: [{ metric: "cpu", share: 1, deviation: 9 }] } });
const warned = (target: "degradation" | "failure", p: number) =>
  opinion({
    risks: [{ target, probability: p, level: "high", horizon_s: 120 }],
    warnings: [{ id: target, target, probability: p, peak: p, opened_at: "2026-01-01T00:00:00Z", horizon_s: 120 }],
    reasons: [{ metric: "memory", text: "memory trend", contribution: 2 }],
  });

describe("opinions", () => {
  it("an unusual moment is not an anomaly until it is confirmed", () => {
    const c = { insight: opinion({ anomaly: { score: 0.8, level: "abnormal", condition: "errors", since: null, contributions: [] } }) };
    expect(isAnomalous(c)).toBe(false);
    expect(isAnomalous({ insight: anomaly(0.8) })).toBe(true);
    expect(isAnomalous({ insight: null })).toBe(false);
  });
  it("a predicted failure comes before a predicted degradation", () => {
    const i = opinion({ warnings: [...warned("degradation", 0.99).warnings, ...warned("failure", 0.6).warnings] });
    expect(topWarning({ insight: i })!.target).toBe("failure");
    expect(topWarning({ insight: opinion() })).toBeNull();
  });
  it("shows a risk in the scene only with a warning", () => {
    expect(riskOf({ insight: opinion() })).toBe(0);
    expect(riskOf({ insight: warned("failure", 0.7) })).toBe(0.7);
    expect(topRisk({ insight: opinion({ risks: [{ target: "failure", probability: null, level: "present", horizon_s: 120 }] }) })).toBeNull();
  });
  it("never claims certainty", () => {
    expect(percent(0.9999)).toBe("> 99 %");
    expect(percent(0.0001)).toBe("< 1 %");
    expect(percent(0.456)).toBe("46 %");
  });
  it("has colours that no status has", () => {
    const used = Object.values(STATUS).map((s) => s.color.toLowerCase());
    for (const c of Object.values(AI_COLOR)) expect(used).not.toContain(c);
  });
});

describe("messages about opinions", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 503 })));
    useTwin.setState({ mirror: null, link: "open", events: [], toasts: [], resyncs: 0 });
    samples.clear();
    const b = bank();
    useTwin.getState().receive(message("snapshot", 1, b.components, b.links));
  });
  const tell = (v: number, insight: Insight | null, ai: "live" | "absent" = "live") => {
    const m: TwinMessage = message("delta", v, [component("a", { insight })]);
    m.summary!.ai = ai;
    useTwin.getState().receive(m);
    return useTwin.getState().toasts;
  };

  it("warns before the failure, once, with probability and reason", () => {
    const t = tell(2, warned("failure", 0.82));
    expect(t.map((x) => [x.status, x.objectId, x.text, x.detail])).toEqual([
      ["predicted", "a", "a Service: failure predicted", "82 % within 2 min, mainly: memory trend"]]);
    expect(tell(3, warned("failure", 0.9))).toHaveLength(1);
    expect(useTwin.getState().mirror!.components.a.status).toBe("healthy");
  });
  it("tells about abnormal behaviour, not about every unusual moment", () => {
    expect(tell(2, anomaly(0.6, "unusual"))).toEqual([]);
    expect(tell(3, opinion())).toEqual([]);
    expect(tell(4, anomaly(0.95)).map((x) => x.status)).toEqual(["anomaly"]);
  });
  it("is silent when the AI service itself comes or goes", () => {
    expect(tell(2, null, "absent")).toEqual([]);
    expect(tell(3, warned("failure", 0.9), "live")).toEqual([]);
  });
  it("keeps the opinions as curves beside the measurements", () => {
    tell(2, opinion());
    tell(3, warned("failure", 0.7));
    expect(samples.get("a", "ai_risk_failure").map((p) => p.v)).toEqual([0.02, 0.7]);
    expect(samples.get("a", "ai_anomaly").map((p) => p.v)).toEqual([0.1, 0.1]);
    expect(samples.get("a", "ai_risk_degradation")).toHaveLength(1);
  });
});

describe("the scene", () => {
  const view: ViewInput = {
    reach: null, mode: "normal", selected: null, selectedLink: null, hovered: null, hiddenTypes: new Set(), statusFilter: null, structural: false,
  };
  it("marks the service and leaves its status alone", () => {
    const b = bank();
    const w = new World();
    const sync = (insight: Insight | null) => {
      b.components[1] = { ...b.components[1], insight };
      const r = apply(null, message("snapshot", 1, b.components, b.links));
      if (r.ok) w.sync(r.mirror);
      for (let i = 0; i < 200; i++) w.step(1 / 60, true);
      return w.nodes.get("a")!;
    };
    w.view(view);
    expect([sync(null).anomaly, sync(null).risk]).toEqual([0, 0]);
    let n = sync(anomaly(0.9));
    expect(n.anomaly).toBeCloseTo(0.9, 2);
    expect(n.risk).toBe(0);
    n = sync(warned("failure", 0.75));
    expect(n.risk).toBeCloseTo(0.75, 2);
    expect(n.anomaly).toBe(0);
    expect(n.status).toBe("healthy");
    expect(`#${n.color.getHexString()}`).toBe(STATUS.healthy.color);
    expect(w.nodes.get("b")!.risk).toBe(0);
    expect(sync(null).risk).toBe(0);
  });
});
