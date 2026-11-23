import { useStatusSummary, useTwinStore } from "../store/twinStore";

const SOURCE_LABELS: Record<string, string> = {
  simulated: "built-in simulation",
  bank_services: "banking services",
};

export function TopBar() {
  const source = useTwinStore((s) => s.source);
  const connection = useTwinStore((s) => s.connection);
  const version = useTwinStore((s) => s.version);
  const nodeCount = useTwinStore((s) => s.topology?.nodes.length ?? 0);
  const summary = useStatusSummary();

  return (
    <header className="topbar">
      <div className="brand">
        <span className="logo">◆</span> SimuCore
        <span className="subtitle">Banking Infrastructure Digital Twin</span>
      </div>
      <div className="topbar-stats">
        <span className={`conn conn-${connection}`}>● {connection}</span>
        <span>twin v{version}</span>
        <span>{nodeCount} nodes</span>
        <span className="status-healthy">{summary.healthy} healthy</span>
        <span className="status-degraded">{summary.degraded} degraded</span>
        <span className="status-critical">{summary.critical} critical</span>
        <span className="status-down">{summary.down} down</span>
        <span className="status-unknown">{summary.unknown} unknown</span>
        <span className="mode-badge">
          {connection === "live" ? "LIVE" : "NOT LIVE"}
          {source && ` · ${SOURCE_LABELS[source] ?? source}`}
        </span>
      </div>
    </header>
  );
}
