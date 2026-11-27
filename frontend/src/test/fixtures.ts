import type { ComponentType, LinkKind, Status, TwinComponent, TwinLink, TwinMessage } from "../twin/types";

export function component(id: string, over: Partial<TwinComponent> & { type?: ComponentType } = {}): TwinComponent {
  return {
    id, type: "microservice", name: `${id} Service`, description: "", criticality: 3, status: "healthy",
    status_since: null, previous_status: null, reason: "", findings: [], root_causes: [], metrics: {},
    data_quality: "complete", attributes: {}, dependencies: { depends_on: [], dependents: [] }, insight: null,
    display: { layer: "application", group: "bank", order: 0, size: 3, load: null, attention: false }, ...over,
  };
}

export function link(source: string, target: string, kind: LinkKind = "calls", over: Partial<TwinLink> = {}): TwinLink {
  return {
    id: `${source}->${target}`, source, target, kind, mode: "required", weight: 1, purpose: "", status: "healthy",
    status_since: null, reason: "", active: false, metrics: {}, data_quality: "complete",
    display: { flow: 0, attention: false }, ...over,
  };
}

export function message(
  type: TwinMessage["type"], version: number, components: TwinComponent[] = [], links: TwinLink[] = [],
  over: { base?: number | null; run?: string; structure?: number; sync?: "live" | "stale"; origin?: "measured" | "synthetic"; at?: string } = {},
): TwinMessage {
  return {
    type, base_version: type === "snapshot" ? null : over.base ?? version - 1,
    meta: {
      api_version: "2.0", twin_id: "t", run_id: over.run ?? "run1", version, structure_version: over.structure ?? 1,
      sync: over.sync ?? "live", origin: over.origin ?? "measured",
      last_observation_at: over.at ?? new Date(1_700_000_000_000 + version * 2000).toISOString(),
      generated_at: "2026-01-01T00:00:00Z",
    },
    summary: type === "heartbeat" ? null : {
      overall: "healthy" as Status, components: components.length, by_status: {}, by_type: {}, links: links.length,
      links_failed: 0, needs_attention: [], ai: "live", anomalies: [], predicted: [],
    },
    components, links, transitions: [],
  };
}

/** A small bank: entry -> a -> b -> db service -> store, all on one host and one network. */
export function bank() {
  const components = [
    component("entry", { type: "load_balancer" }), component("a"), component("b"), component("dbs"),
    component("store", { type: "database" }), component("host", { type: "server" }),
    component("net", { type: "network" }), component("mon", { type: "monitoring", display: { layer: "platform", group: "platform", order: 0, size: 2, load: null, attention: false } }),
  ];
  const links = [
    link("entry", "a", "routes_to"), link("a", "b"), link("a", "dbs"), link("b", "dbs"), link("dbs", "store", "stores_in"),
    link("a", "host", "hosted_on"), link("b", "host", "hosted_on"), link("dbs", "host", "hosted_on"),
    link("a", "net", "connected_to"), link("b", "net", "connected_to"), link("mon", "store", "monitors"),
  ];
  return { components, links };
}

export const byId = <T extends { id: string }>(list: T[]): Record<string, T> => Object.fromEntries(list.map((x) => [x.id, x]));
