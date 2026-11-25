import type { ComponentType, LinkKind, Status } from "./types";

export const STATUS: Record<Status, { label: string; color: string; icon: string; rank: number }> = {
  failed: { label: "Failed", color: "#f0475f", icon: "cross", rank: 5 },
  overloaded: { label: "Overloaded", color: "#f58236", icon: "gauge", rank: 4 },
  warning: { label: "Warning", color: "#f2c230", icon: "triangle", rank: 3 },
  recovering: { label: "Recovering", color: "#5aa2ff", icon: "cycle", rank: 2 },
  unknown: { label: "Unknown", color: "#8b97a8", icon: "question", rank: 1 },
  healthy: { label: "Healthy", color: "#22b573", icon: "check", rank: 0 },
};

export const bySeverity = (a: Status, b: Status) => STATUS[b].rank - STATUS[a].rank;

export const TYPE: Record<ComponentType, { label: string; plural: string }> = {
  load_balancer: { label: "Load balancer", plural: "Load balancers" },
  microservice: { label: "Banking microservice", plural: "Banking microservices" },
  database: { label: "Database", plural: "Databases" },
  server: { label: "Application server", plural: "Application servers" },
  network: { label: "Network", plural: "Network" },
  gateway: { label: "External gateway", plural: "External gateways" },
  monitoring: { label: "Monitoring", plural: "Monitoring" },
};
export const TYPE_ORDER: ComponentType[] = [
  "load_balancer", "microservice", "database", "gateway", "server", "network", "monitoring",
];

export const KIND: Record<LinkKind, string> = {
  calls: "calls",
  routes_to: "routes to",
  stores_in: "stores in",
  hosted_on: "runs on",
  connected_to: "connected to",
  monitors: "collects from",
};

export const METRIC: Record<string, string> = {
  cpu_pct: "CPU", mem_pct: "Memory", mem_bytes: "Memory used", latency_ms: "Latency (p95)",
  latency_mean_ms: "Latency (mean)", rps: "Requests", error_rate: "Error rate",
  net_rx_bps: "Network in", net_tx_bps: "Network out", db_response_ms: "Database time",
  active_connections: "Open connections", queue_length: "Queue", in_flight: "In progress",
  availability: "Availability", db_pool_in_use: "Pool in use", db_pool_size: "Pool size",
  db_failure_rate: "Refused queries", calls_per_s: "Calls", failure_rate: "Failures",
  circuit: "Circuit breaker", connections: "Connections", connections_failed: "Failed connections",
  bytes_per_s: "Traffic", load1: "Load (1 min)", disk_read_bps: "Disk read", disk_write_bps: "Disk write",
  database_bytes: "Database size", commits_per_s: "Commits", cache_hit_ratio: "Cache hits",
  targets_up: "Targets up", targets_total: "Targets", slowest_scrape_s: "Slowest scrape",
  series: "Time series", hosted_services: "Services hosted", hosted_running: "Services running",
  hosted_cpu_pct: "CPU of services", hosted_mem_bytes: "Memory of services",
  ingress_rps: "Customer requests", routes: "Routes", routes_failed: "Failed routes",
};
export const metricLabel = (m: string) => METRIC[m] ?? m.replace(/_/g, " ");

/** Shown first, in this order. */
export const KEY_METRICS = [
  "cpu_pct", "mem_pct", "latency_ms", "ingress_rps", "rps", "calls_per_s", "error_rate", "failure_rate",
  "queue_length", "in_flight", "db_pool_in_use", "db_response_ms", "hosted_cpu_pct", "hosted_mem_bytes",
  "connections", "connections_failed", "bytes_per_s",
];

const num = (v: number, digits: number) =>
  v.toLocaleString("en-GB", { minimumFractionDigits: 0, maximumFractionDigits: digits });

function scaled(v: number, unit: string): [string, string] {
  const steps: [number, string][] = [[1 << 30, "GB"], [1 << 20, "MB"], [1 << 10, "KB"]];
  for (const [size, name] of steps) if (Math.abs(v) >= size) return [num(v / size, v / size < 10 ? 1 : 0), name + unit];
  return [num(v, 0), "B" + unit];
}

/** A value with its unit. The unit comes from the backend's catalogue. */
export function formatMetric(metric: string, v: number, units: Record<string, string>): [string, string] {
  const unit = units[metric] ?? "";
  if (metric === "circuit") return [v >= 2 ? "open" : v >= 1 ? "half open" : "closed", ""];
  if (unit === "ratio") return [num(v * 100, v !== 0 && Math.abs(v) < 0.1 ? 2 : 1), "%"];
  if (unit === "B") return scaled(v, "");
  if (unit === "B/s") return scaled(v, "/s");
  if (unit === "ms" && v >= 1000) return [num(v / 1000, 2), "s"];
  return [num(v, Math.abs(v) >= 100 ? 0 : Math.abs(v) >= 10 ? 1 : 2), unit];
}

export const text = (metric: string, v: number | undefined, units: Record<string, string>) =>
  v === undefined ? "–" : formatMetric(metric, v, units).filter(Boolean).join(" ");

export function ago(iso: string | null, now: number): string {
  if (!iso) return "–";
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${s % 60} s`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  return `${Math.floor(s / 86400)} d`;
}

export const clock = (iso: string | number) =>
  new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
