import { useMemo, useState } from "react";
import { api } from "../services/api";
import { useTwinStore } from "../store/twinStore";

function formatRate(bytesPerSecond: number): string {
  if (bytesPerSecond >= 1e6) return `${(bytesPerSecond / 1e6).toFixed(1)} MB/s`;
  if (bytesPerSecond >= 1e3) return `${(bytesPerSecond / 1e3).toFixed(1)} kB/s`;
  return `${bytesPerSecond.toFixed(0)} B/s`;
}

const METRIC_LABELS: Record<string, [string, (v: number) => string]> = {
  cpu_pct: ["CPU", (v) => `${v.toFixed(1)} %`],
  mem_pct: ["Memory", (v) => `${v.toFixed(1)} %`],
  latency_ms: ["Latency p95", (v) => `${v.toFixed(1)} ms`],
  error_rate: ["Error rate", (v) => `${(v * 100).toFixed(2)} %`],
  rps: ["Throughput", (v) => `${v.toFixed(0)} req/s`],
  net_rx_bps: ["Network in", formatRate],
  net_tx_bps: ["Network out", formatRate],
  db_response_ms: ["DB response", (v) => `${v.toFixed(1)} ms`],
  active_connections: ["Connections", (v) => v.toFixed(0)],
  queue_length: ["Queue", (v) => v.toFixed(0)],
  availability: ["Availability", (v) => (v > 0 ? "up" : "DOWN")],
};

export function Inspector() {
  const selectedId = useTwinStore((s) => s.selectedId);
  const topology = useTwinStore((s) => s.topology);
  const state = useTwinStore((s) => (selectedId ? s.states[selectedId] : undefined));
  const { select, setImpact } = useTwinStore.getState();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const node = topology?.nodes.find((n) => n.id === selectedId);
  const { deps, dependents } = useMemo(() => {
    const edges = topology?.edges ?? [];
    return {
      deps: [...new Set(edges.filter((e) => e.source === selectedId).map((e) => e.target))],
      dependents: [...new Set(edges.filter((e) => e.target === selectedId).map((e) => e.source))],
    };
  }, [topology, selectedId]);

  if (!node) {
    return (
      <aside className="panel inspector">
        <h3>Inspector</h3>
        <p className="muted">Click a component in the 3D scene to inspect it.</p>
      </aside>
    );
  }

  const simulateFailure = async () => {
    setBusy(true);
    setError(null);
    try {
      setImpact(await api.analyzePropagation([node.id]));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <aside className="panel inspector">
      <h3>{node.name}</h3>
      <div className="kv">
        <span>ID</span><code>{node.id}</code>
        <span>Kind</span><span>{node.kind}</span>
        <span>Layer</span><span>{node.layer}</span>
        <span>Criticality</span><span>{"★".repeat(node.criticality)}</span>
        <span>Status</span><span className={`status status-${state?.status ?? "unknown"}`}>{state?.status ?? "unknown"}</span>
      </div>
      <h4>Live metrics</h4>
      <div className="kv">
        {Object.entries(METRIC_LABELS).map(([k, [label, fmt]]) =>
          state?.metrics[k] !== undefined ? (
            <FragmentRow key={k} label={label} value={fmt(state.metrics[k])} />
          ) : null,
        )}
      </div>
      <h4>Depends on ({deps.length})</h4>
      <div className="chips">{deps.map((d) => <button key={d} className="chip dep" onClick={() => select(d)}>{d}</button>)}</div>
      <h4>Depended on by ({dependents.length})</h4>
      <div className="chips">{dependents.map((d) => <button key={d} className="chip dependent" onClick={() => select(d)}>{d}</button>)}</div>
      <button className="primary" disabled={busy} onClick={simulateFailure}>
        {busy ? "Analysing…" : "Simulate failure → blast radius"}
      </button>
      {error && <p className="error">{error}</p>}
    </aside>
  );
}

function FragmentRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <span>{label}</span>
      <span>{value}</span>
    </>
  );
}
