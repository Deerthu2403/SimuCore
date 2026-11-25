import { useMemo, useState } from "react";
import { ago, bySeverity, clock, STATUS } from "../twin/format";
import { useMonitoring } from "../twin/monitoring";
import { useTwin, type HistoryEvent } from "../twin/store";
import { STATUSES, type Status } from "../twin/types";
import { useView } from "../twin/view";
import { useNow } from "./hooks";
import { Icon, StatusChip, StatusDot } from "./Icon";

/** How the names of objects look to people. Objects that no longer exist keep their id. */
function useName(): (kind: "component" | "link", id: string) => string {
  const m = useTwin((s) => s.mirror);
  return (kind, id) => {
    const short = (x: string) => (m?.components[x]?.name ?? x).replace(/ Service$/, "");
    if (kind === "component") return m?.components[id]?.name ?? id;
    const [a, b] = id.split("->");
    return `${short(a)} → ${short(b)}`;
  };
}

// --------------------------------------------------------------------- events
export function EventList({ objectId }: { objectId?: string }) {
  const events = useTwin((s) => s.events);
  const state = useTwin((s) => s.historyState);
  const [kind, setKind] = useState<"component" | "all">("component");
  const [status, setStatus] = useState<Status | "any">("any");
  const name = useName();
  const { select } = useView.getState();
  const shown = useMemo(() => events.filter((e) =>
    (objectId ? e.object_id === objectId || e.object_id.startsWith(`${objectId}->`) || e.object_id.endsWith(`->${objectId}`) : true)
    && (kind === "all" || e.object_kind === "component") && (status === "any" || e.to_status === status)).slice(0, 150),
  [events, objectId, kind, status]);
  return (
    <div className="events" data-testid={objectId ? "object-events" : "event-history"}>
      <div className="filters">
        <label>Show <select value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
          <option value="component">components</option><option value="all">components and connections</option></select></label>
        <label>Status <select value={status} onChange={(e) => setStatus(e.target.value as typeof status)}>
          <option value="any">any</option>{STATUSES.map((s) => <option key={s} value={s}>{STATUS[s].label}</option>)}</select></label>
        {state === "unavailable" && <span className="note warn">Older events are unavailable; only those since this page was opened are listed.</span>}
      </div>
      {shown.length === 0 && <p className="empty">{state === "loading" ? "Loading…" : "No events."}</p>}
      <ol>
        {shown.map((e) => (
          <li key={e.key}>
            <button type="button" className="event" data-object={e.object_id} data-to={e.to_status} onClick={() => select({ kind: e.object_kind, id: e.object_id }, { fly: true })}>
              <time dateTime={e.ts}>{clock(e.ts)}</time>
              <Change e={e} />
              <span className="grow">{name(e.object_kind, e.object_id)}</span>
              <span className="muted why">{e.reason}</span>
            </button>
          </li>
        ))}
      </ol>
    </div>
  );
}

function Change({ e }: { e: HistoryEvent }) {
  return (
    <span className="change">
      {e.from_status ? <StatusDot status={e.from_status} /> : <span className="muted">new</span>}
      <span className="muted">→</span>
      <StatusChip status={e.to_status} small />
    </span>
  );
}

// --------------------------------------------------------------------- alerts
export function useAlerts() {
  const m = useTwin((s) => s.mirror);
  return useMemo(() => {
    if (!m) return [];
    const cs = Object.values(m.components).filter((c) => c.status !== "healthy" && c.status !== "unknown")
      .map((c) => ({ kind: "component" as const, id: c.id, status: c.status, since: c.status_since, reason: c.reason, causes: c.root_causes.filter((r) => r !== c.id) }));
    const ks = Object.values(m.links).filter((k) => k.kind === "calls" && (k.status === "failed" || k.status === "warning"))
      .map((k) => ({ kind: "link" as const, id: k.id, status: k.status, since: k.status_since, reason: k.reason, causes: [] as string[] }));
    return [...cs, ...ks].sort((a, b) => bySeverity(a.status, b.status)
      || (a.kind === b.kind ? Date.parse(b.since ?? "0") - Date.parse(a.since ?? "0") : a.kind === "component" ? -1 : 1));
  }, [m]);
}

export function Alerts() {
  const alerts = useAlerts();
  const unknown = useTwin((s) => s.mirror?.summary.by_status.unknown ?? 0);
  const open = useMonitoring((s) => s.open);
  const monitoring = useMonitoring((s) => s.connected);
  const name = useName();
  const now = useNow();
  const { select, setStatusFilter } = useView.getState();
  const alarms = Object.values(open).sort((a, b) => Date.parse(b.opened_at) - Date.parse(a.opened_at));
  return (
    <div className="alerts" data-testid="alerts">
      <section aria-label="Alerts of the digital twin">
        <h3>Digital twin <span className="count">{alerts.length}</span></h3>
        {alerts.length === 0 && <p className="empty">{unknown ? `No alerts, but the status of ${unknown} is unknown.` : "No alerts. Everything works."}</p>}
        {unknown > 0 && alerts.length > 0 && (
          <button type="button" className="linklike" onClick={() => setStatusFilter("unknown")}>Status unknown: {unknown}</button>
        )}
        <ul>
          {alerts.map((a) => (
            <li key={a.id}>
              <button type="button" className="event" data-object={a.id} data-status={a.status} onClick={() => select({ kind: a.kind, id: a.id }, { fly: true })}>
                <StatusChip status={a.status} small />
                <span className="grow">{name(a.kind, a.id)}</span>
                <span className="muted why">{a.reason}{a.causes.length ? ` (cause: ${a.causes.map((c) => name("component", c)).join(", ")})` : ""}</span>
                <time>{ago(a.since, now)}</time>
              </button>
            </li>
          ))}
        </ul>
      </section>
      <section aria-label="Open alarms of monitoring">
        <h3>Monitoring <span className="count">{alarms.length}</span></h3>
        {!monitoring && <p className="note">The monitoring stream is not connected.</p>}
        {monitoring && alarms.length === 0 && <p className="empty">No open alarms.</p>}
        <ul>
          {alarms.map((e) => (
            <li key={e.id} className="event static">
              <Icon name={e.severity === "critical" || e.severity === "high" ? "cross" : "triangle"} size={13} color={STATUS[e.severity === "critical" || e.severity === "high" ? "failed" : "warning"].color} />
              <span className="sev">{e.severity}</span>
              <span className="grow why">{e.service ? `${e.service}: ` : ""}{e.message}</span>
              <time>{ago(e.opened_at, now)}</time>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
