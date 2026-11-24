import { useMonitoringStore } from "../store/monitoringStore";
import { useTwinStore } from "../store/twinStore";

const ORDER = ["critical", "high", "medium", "low", "info"];

export function MonitoringPanel() {
  const { connected, fresh, origin, overview, nodeOf, open } = useMonitoringStore();
  const select = useTwinStore((s) => s.select);
  const known = useTwinStore((s) => s.topology?.nodes);
  if (!connected || !overview) return null;

  const events = Object.values(open).sort(
    (a, b) => ORDER.indexOf(a.severity) - ORDER.indexOf(b.severity) || a.opened_at.localeCompare(b.opened_at),
  );
  const nodeFor = (service: string | null) => {
    const id = service ? nodeOf[service] : undefined;
    return id && known?.some((n) => n.id === id) ? id : undefined;
  };

  return (
    <section className="panel monitoring" aria-label="Monitoring">
      <h3>
        Monitoring {!fresh && <span className="status-critical">· data out of date</span>}
        {origin === "synthetic" && <span className="status-degraded"> · synthetic</span>}
      </h3>
      <p className="muted">
        {overview.up}/{overview.services} services up · {overview.total_rps.toFixed(1)} req/s
        {overview.error_rate !== null && ` · ${(overview.error_rate * 100).toFixed(1)} % errors`}
      </p>
      {events.length === 0 ? (
        <p className="muted">No open problems.</p>
      ) : (
        <ul className="event-list">
          {events.slice(0, 12).map((e) => {
            const node = nodeFor(e.service);
            const text = `${e.service ?? "monitoring"}: ${e.message}`;
            return (
              <li key={e.id} className={`sev-${e.severity}`} title={text}>
                <span className="sev">{e.severity}</span>
                {node ? <button className="link" onClick={() => select(node)}>{text}</button> : <span>{text}</span>}
              </li>
            );
          })}
          {events.length > 12 && <li className="muted">and {events.length - 12} more</li>}
        </ul>
      )}
    </section>
  );
}
