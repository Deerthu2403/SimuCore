import { create } from "zustand";

/** Open alarms of the monitoring module (monitoring/monitor/quality.py). Optional: the twin works
 *  without it, and nothing here reaches the 3D scene. */
export interface MonitoringEvent {
  id: string;
  kind: string;
  severity: "info" | "low" | "medium" | "high" | "critical";
  service: string | null;
  metric: string | null;
  message: string;
  opened_at: string;
  closed_at: string | null;
}

interface Message {
  type: "snapshot" | "update";
  /** Snapshot: all open events. Update: only those that opened or closed. */
  events: MonitoringEvent[];
}

interface Store {
  connected: boolean;
  open: Record<string, MonitoringEvent>;
}

export const useMonitoring = create<Store>(() => ({ connected: false, open: {} }));

export function receiveMonitoring(m: Message): void {
  if (m.type === "update" && m.events.length === 0) return;
  useMonitoring.setState((s) => {
    const open = m.type === "snapshot" ? {} : { ...s.open };
    for (const e of m.events) {
      if (e.closed_at) delete open[e.id];
      else open[e.id] = e;
    }
    return { open };
  });
}

export function connectMonitoring(): () => void {
  let ws: WebSocket | null = null;
  let retry = 0;
  let closed = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const open = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/monitoring/ws/metrics`);
    ws.onopen = () => {
      retry = 0;
      useMonitoring.setState({ connected: true });
    };
    ws.onmessage = (ev) => receiveMonitoring(JSON.parse(ev.data as string) as Message);
    ws.onclose = () => {
      if (closed) return;
      // Alarms that cannot be followed any more are not shown as if they were open.
      useMonitoring.setState({ connected: false, open: {} });
      timer = setTimeout(open, Math.min(2000 * 2 ** retry++, 30_000));
    };
  };
  open();
  return () => {
    closed = true;
    clearTimeout(timer);
    ws?.close();
  };
}
