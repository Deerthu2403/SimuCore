import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { depths, neighbours, reach, rows, search } from "../twin/graph";
import { ago, formatMetric, text } from "../twin/format";
import { apply, type Mirror } from "../twin/mirror";
import { receiveMonitoring, useMonitoring } from "../twin/monitoring";
import * as samples from "../twin/samples";
import { connectTwin, SILENCE_MS } from "../twin/socket";
import { freshness, mergeEvents, onChanges, useTwin } from "../twin/store";
import type { Transition, TwinMessage } from "../twin/types";
import { currentReach, useView } from "../twin/view";
import { niceMax, segments } from "../ui/Sparkline";
import { bank, byId, component, link, message } from "./fixtures";

const start = (): Mirror => {
  const b = bank();
  const r = apply(null, message("snapshot", 1, b.components, b.links));
  if (!r.ok) throw new Error(r.why);
  return r.mirror;
};

describe("mirror", () => {
  it("applies a delta and keeps the identity of everything else", () => {
    const m = start();
    const r = apply(m, message("delta", 2, [component("a", { status: "failed" })]));
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.mirror.components.a.status).toBe("failed");
    expect(r.changes).toEqual([{ kind: "component", id: "a", from: "healthy", to: "failed" }]);
    expect(r.mirror.components.b).toBe(m.components.b);
    expect(r.mirror.links).toBe(m.links);
    expect(r.mirror.componentIds).toBe(m.componentIds);
    expect(m.components.a.status).toBe("healthy"); // the old copy is untouched
  });
  it.each([
    ["a gap", message("delta", 5, [], [], { base: 4 })],
    ["another run", message("delta", 2, [], [], { run: "run2" })],
    ["another structure", message("delta", 2, [], [], { structure: 2 })],
    ["an unknown component", message("delta", 2, [component("nobody")])],
    ["an unknown link", message("delta", 2, [], [link("x", "y")])],
    ["a heartbeat with a newer version", message("heartbeat", 3, [], [], { base: 1 })],
  ])("refuses %s", (_, m) => expect(apply(start(), m).ok).toBe(false));
  it("refuses a delta before the first snapshot", () => expect(apply(null, message("delta", 2)).ok).toBe(false));
  it("takes the meta of a heartbeat", () => {
    const r = apply(start(), message("heartbeat", 1, [], [], { base: 1, sync: "stale" }));
    expect(r.ok && r.mirror.meta.sync).toBe("stale");
  });
  it("finds the changes between two snapshots, keeps ids if the structure is the same", () => {
    const m = start();
    const b = bank();
    b.components[1].status = "overloaded";
    const r = apply(m, message("snapshot", 9, b.components, b.links));
    expect(r.ok && r.changes.map((c) => c.id)).toEqual(["a"]);
    expect(r.ok && r.mirror.componentIds).toBe(m.componentIds);
    expect(r.ok && r.structure).toBe(false);
  });
  it("sees a structure change, a restart, and drops links to nowhere", () => {
    const b = bank();
    const r = apply(start(), message("snapshot", 9, [...b.components, component("new")], [...b.links, link("new", "ghost")], { run: "run2" }));
    expect(r.ok && r.structure && r.restarted).toBe(true);
    expect(r.ok && "new->ghost" in r.mirror.links).toBe(false);
  });
});

// ------------------------------------------------------------------- socket
class FakeSocket {
  static all: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  closed = false;
  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
  send(s: string) {
    this.sent.push(s);
  }
  close() {
    this.closed = true;
  }
}

describe("socket", () => {
  beforeEach(() => {
    FakeSocket.all = [];
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeSocket);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });
  const run = (ok: boolean | (() => boolean)) => {
    const states: string[] = [];
    const stop = connectTwin("ws://x", { onMessage: () => (typeof ok === "function" ? ok() : ok), onState: (s) => states.push(s) });
    return { states, stop };
  };
  const last = () => FakeSocket.all[FakeSocket.all.length - 1];
  const say = (type: string) => last().onmessage!({ data: JSON.stringify({ type }) });

  it("reconnects with growing waits after a loss", () => {
    const { states } = run(true);
    last().onopen!();
    last().onclose!();
    expect(states).toEqual(["connecting", "open", "closed"]);
    vi.advanceTimersByTime(700);
    expect(FakeSocket.all).toHaveLength(2);
    last().onclose!();
    vi.advanceTimersByTime(600);
    expect(FakeSocket.all).toHaveLength(2);
    vi.advanceTimersByTime(700);
    expect(FakeSocket.all).toHaveLength(3);
  });
  it("treats silence as a dead line", () => {
    const { states } = run(true);
    last().onopen!();
    say("snapshot");
    vi.advanceTimersByTime(SILENCE_MS - 100);
    expect(states[states.length - 1]).toBe("open");
    vi.advanceTimersByTime(200);
    expect(states[states.length - 1]).toBe("closed");
    expect(FakeSocket.all[0].closed).toBe(true);
  });
  it("asks once for a resync until a snapshot arrives", () => {
    let ok = false;
    run(() => ok);
    last().onopen!();
    say("delta");
    say("delta");
    expect(last().sent).toEqual(['{"type":"resync"}']);
    ok = true;
    say("snapshot");
    ok = false;
    say("delta");
    expect(last().sent).toHaveLength(2);
  });
  it("drops a connection that sends nonsense, and stops when told to", () => {
    const { stop } = run(true);
    last().onmessage!({ data: "{" });
    expect(FakeSocket.all[0].closed).toBe(true);
    stop();
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.all).toHaveLength(1);
  });
});

// ------------------------------------------------------------------ samples
describe("samples", () => {
  beforeEach(() => samples.clear());
  const at = (n: number) => new Date(1_700_000_000_000 + n).toISOString();
  const rec = (n: number, cpu?: number, sync: "live" | "stale" = "live") => {
    const r = apply(null, message("snapshot", 1, [component("a", { metrics: cpu === undefined ? {} : { cpu_pct: cpu } })], [], { at: at(n), sync }));
    if (r.ok) samples.record(r.mirror);
  };
  it("keeps one point per measurement and leaves gaps where there is no value", () => {
    rec(0, 1);
    rec(0, 1); // the same measurement again
    rec(2000);
    rec(4000, 3);
    expect(samples.get("a", "cpu_pct").map((p) => p.v)).toEqual([1, 3]);
  });
  it("records nothing while the twin is not live", () => {
    rec(0, 1);
    rec(2000, 2, "stale");
    expect(samples.get("a", "cpu_pct")).toHaveLength(1);
  });
  it("forgets after ten minutes", () => {
    rec(0, 1);
    rec(11 * 60_000, 2);
    expect(samples.get("a", "cpu_pct").map((p) => p.v)).toEqual([2]);
  });
  it("puts history before the live points, without duplicates", () => {
    rec(10_000, 5);
    samples.prefill("a", "cpu_pct", [{ t: 1_700_000_008_000, v: 4 }, { t: 1_700_000_006_000, v: 3 }, { t: 1_700_000_010_000, v: 9 }, { t: 1_700_000_004_000, v: NaN }]);
    expect(samples.get("a", "cpu_pct").map((p) => p.v)).toEqual([3, 4, 5]);
  });
  it("draws holes as holes", () => {
    const p = [0, 2, 4, 30, 32].map((s) => ({ t: s * 1000, v: s }));
    expect(segments(p).map((s) => s.length)).toEqual([3, 2]);
  });
  it.each([[0, 1], [0.7, 1], [3, 5], [11, 20], [24, 25], [100, 100], [260, 500], [0.012, 0.02]])("scale for %s is %s", (v, top) => expect(niceMax(v)).toBeCloseTo(top, 9));
});

// -------------------------------------------------------------------- graph
describe("graph", () => {
  const b = bank();
  const links = byId(b.links);
  it("finds what a component needs (+) and what needs it (-), with the fewest steps", () => {
    const r = reach("b", links);
    expect(Object.fromEntries(r.steps)).toEqual({ b: 0, dbs: 1, host: 1, store: 2, a: -1, entry: -2 });
    expect(r.links.has("a->net")).toBe(false);
  });
  it("lays out rows so that every link points down", () => {
    for (const c of ["entry", "a", "b", "dbs", "store"]) {
      const r = reach(c, links);
      const row = rows(c, r, links);
      expect(row.get(c)).toBe(0);
      expect(row.size).toBe(r.steps.size);
      for (const id of r.links) expect(row.get(links[id].source)!).toBeLessThan(row.get(links[id].target)!);
    }
  });
  it("finds direct neighbours over every kind of link", () => {
    expect([...neighbours("a", links).steps.keys()].sort()).toEqual(["a", "b", "dbs", "entry", "host", "net"]);
  });
  it("measures depth by the longest path and survives cycles", () => {
    const d = depths(["entry", "a", "b", "dbs"], links, new Set(["calls", "routes_to"]));
    expect(Object.fromEntries(d)).toEqual({ entry: 0, a: 1, b: 2, dbs: 3 });
    const loop = byId([link("x", "y"), link("y", "x")]);
    expect(() => depths(["x", "y"], loop, new Set(["calls"]))).not.toThrow();
    expect(() => reach("x", loop)).not.toThrow();
  });
  it("searches by every word, best match first", () => {
    const list = [
      component("bank-payment", { name: "Payment Gateway Service", attributes: { port: 8105 } }),
      component("gw-external", { name: "External payment network", type: "gateway" }),
      component("bank-fraud", { name: "Fraud Detection Service", status: "failed" }),
    ];
    const ids = (q: string) => search(q, list).map((c) => c.id);
    expect(ids("pay")).toEqual(["bank-payment", "gw-external"]);
    expect(ids("8105")).toEqual(["bank-payment"]);
    expect(ids("failed")).toEqual(["bank-fraud"]);
    expect(ids("PAYMENT net")).toEqual(["gw-external"]);
    expect(ids("xyz")).toEqual([]);
    expect(ids("  ")).toHaveLength(3);
  });
});

// ------------------------------------------------------------------- format
describe("format", () => {
  const units = { cpu_pct: "%", latency_ms: "ms", error_rate: "ratio", mem_bytes: "B", bytes_per_s: "B/s", rps: "req/s" };
  it.each([
    ["cpu_pct", 18.44, "18.4 %"], ["latency_ms", 1725, "1.73 s"], ["latency_ms", 24.68, "24.7 ms"], ["error_rate", 0.355, "35.5 %"],
    ["error_rate", 0.0052, "0.52 %"], ["error_rate", 0, "0 %"], ["mem_bytes", 50 * 1024 * 1024, "50 MB"], ["bytes_per_s", 900, "900 B/s"],
    ["circuit", 2, "open"], ["circuit", 0, "closed"], ["rps", 0, "0 req/s"], ["nobody_knows", 1.234, "1.23"],
  ])("%s %s is written %s", (m, v, s) => expect(text(m, v, units)).toBe(s));
  it("never invents a number for a missing value", () => expect(text("cpu_pct", undefined, units)).toBe("–"));
  it("takes units from the catalogue", () => expect(formatMetric("new_metric", 3, { new_metric: "apples" })).toEqual(["3", "apples"]));
  it("writes durations", () => {
    const now = Date.parse("2026-01-01T01:00:00Z");
    expect(ago("2026-01-01T00:59:48Z", now)).toBe("12 s");
    expect(ago("2026-01-01T00:57:30Z", now)).toBe("2 min 30 s");
    expect(ago(null, now)).toBe("–");
  });
});

// -------------------------------------------------------------------- store
describe("store", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 503 })));
    useTwin.setState({ mirror: null, link: "connecting", events: [], toasts: [], resyncs: 0 });
    useView.getState().reset();
    useView.setState({ searchOpen: false, right: true });
    samples.clear();
  });
  afterEach(() => vi.unstubAllGlobals());
  const receive = (m: TwinMessage) => useTwin.getState().receive(m);
  const snapshot = () => {
    const b = bank();
    return receive(message("snapshot", 1, b.components, b.links));
  };
  const t = (over: Partial<Transition>): Transition => ({
    ts: "2026-01-01T00:00:00Z", version: 1, object_kind: "component", object_id: "a", from_status: "healthy",
    to_status: "failed", reason: "", root_causes: [], origin: "measured", ...over,
  });

  it("warns about failures and overloads at once, and tells when they are over", () => {
    snapshot();
    receive(message("delta", 2, [component("a", { status: "failed", reason: "gone" }), component("b", { status: "overloaded" }), component("dbs", { status: "warning" })]));
    expect(useTwin.getState().toasts.map((x) => [x.objectId, x.status, x.detail])).toEqual([["a", "failed", "gone"], ["b", "overloaded", ""]]);
    receive(message("delta", 3, [component("a", { status: "recovering" })]));
    receive(message("delta", 4, [component("a", { status: "healthy" })]));
    expect(useTwin.getState().toasts.map((x) => x.text)).toContain("a Service is healthy again");
  });
  it("does not warn about every component when the twin goes stale", () => {
    snapshot();
    receive(message("delta", 2, [component("a", { status: "failed" })], [], { sync: "stale" }));
    expect(useTwin.getState().toasts).toEqual([]);
  });
  it("does not warn about what was already so when the page opened", () => {
    const b = bank();
    b.components[1].status = "failed";
    receive(message("snapshot", 1, b.components, b.links));
    expect(useTwin.getState().toasts).toEqual([]);
  });
  it("tells the scene about changes in the same tick", () => {
    snapshot();
    const seen: string[] = [];
    const off = onChanges((c) => seen.push(...c.map((x) => x.id)));
    receive(message("delta", 2, [component("a", { status: "failed" })]));
    off();
    expect(seen).toEqual(["a"]);
  });
  it("refuses what it cannot apply and keeps the last good state", () => {
    snapshot();
    expect(receive(message("delta", 9, [component("a", { status: "failed" })], [], { base: 8 }))).toBe(false);
    expect(useTwin.getState().mirror!.components.a.status).toBe("healthy");
    expect(useTwin.getState().resyncs).toBe(1);
  });
  it("never calls old data live", () => {
    expect(freshness(useTwin.getState())).toBe("connecting");
    useTwin.getState().setLink("open");
    snapshot();
    expect(freshness(useTwin.getState())).toBe("live");
    receive(message("heartbeat", 1, [], [], { base: 1, sync: "stale" }));
    expect(freshness(useTwin.getState())).toBe("stale");
    useTwin.getState().setLink("closed");
    expect(freshness(useTwin.getState())).toBe("offline");
  });
  it("merges live and stored events without duplicates, newest first", () => {
    const live = mergeEvents([], [t({ version: 2, ts: "2026-01-01T00:00:02Z", to_status: "recovering" }), t({})], "r");
    const all = mergeEvents(live, [t({ run_id: "r" }), t({ run_id: "older", version: 1, ts: "2025-12-31T00:00:00Z" })], "r");
    expect(all.map((e) => [e.run_id, e.to_status])).toEqual([["r", "recovering"], ["r", "failed"], ["older", "failed"]]);
  });
  it("keeps component events although connections change much more often", () => {
    const flood = Array.from({ length: 900 }, (_, i) => t({ object_kind: "link", object_id: `x->y${i}`, version: 10 + i, ts: new Date(1_800_000_000_000 + i).toISOString() }));
    const all = mergeEvents([], [...flood, t({})], "r");
    expect(all.filter((e) => e.object_kind === "component")).toHaveLength(1);
    expect(all.length).toBeLessThan(600);
  });
  it("starts new curves when the twin restarts or the data comes from elsewhere", () => {
    const b = bank();
    b.components[1].metrics = { cpu_pct: 5 };
    receive(message("snapshot", 1, b.components, b.links));
    expect(samples.get("a", "cpu_pct")).toHaveLength(1);
    receive(message("snapshot", 1, b.components, b.links, { origin: "synthetic", at: new Date(1_700_000_004_000).toISOString() }));
    expect(samples.get("a", "cpu_pct")).toHaveLength(1);
  });

  it("isolates, explores dependencies, and shows everything again", () => {
    snapshot();
    const v = () => useView.getState();
    v().setMode("isolate");
    expect(v().mode).toBe("normal"); // nothing selected
    v().select({ kind: "component", id: "b" });
    v().setMode("isolate");
    expect([...currentReach()!.steps.keys()].sort()).toEqual(["a", "b", "dbs", "host", "net"]);
    v().setMode("dependencies");
    expect(currentReach()!.steps.get("entry")).toBe(-2);
    expect(v().camera.req).toEqual({ what: "fit", ids: expect.arrayContaining(["b", "entry"]) });
    v().select({ kind: "component", id: "dbs" }); // moves the centre, keeps the mode
    expect(v().mode).toBe("dependencies");
    expect(currentReach()!.steps.get("a")).toBe(-1);
    v().select({ kind: "link", id: "a->b" });
    expect(v().mode).toBe("normal");
    v().reset();
    expect(v().selected).toBeNull();
    expect(currentReach()).toBeNull();
  });
  it("flies to what is selected from a list", () => {
    snapshot();
    useView.getState().select({ kind: "link", id: "a->b" }, { fly: true });
    expect(useView.getState().camera.req).toEqual({ what: "fit", ids: ["a", "b"] });
  });
});

describe("monitoring alarms", () => {
  const e = (id: string, closed: string | null = null) => ({ id, kind: "x", severity: "high" as const, service: null, metric: null, message: "", opened_at: "", closed_at: closed });
  it("opens and closes", () => {
    receiveMonitoring({ type: "snapshot", events: [e("1"), e("2")] });
    receiveMonitoring({ type: "update", events: [e("1", "now"), e("3")] });
    expect(Object.keys(useMonitoring.getState().open).sort()).toEqual(["2", "3"]);
    receiveMonitoring({ type: "snapshot", events: [] });
    expect(useMonitoring.getState().open).toEqual({});
  });
});
