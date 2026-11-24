import { useMonitoringStore, type MonitoringMessage } from "../store/monitoringStore";

/** Connects to the monitoring service. It is optional: the twin works without it. */
export function connectMonitoring(): () => void {
  let ws: WebSocket | null = null;
  let retry = 0;
  let closed = false;
  let timer: number | undefined;
  const store = useMonitoringStore.getState;

  const open = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/monitoring/ws/metrics`);
    ws.onopen = () => {
      retry = 0;
      store().setConnected(true);
    };
    ws.onmessage = (ev) => store().receive(JSON.parse(ev.data) as MonitoringMessage);
    ws.onclose = () => {
      if (closed) return;
      store().setConnected(false);
      timer = window.setTimeout(open, Math.min(2000 * 2 ** retry++, 30000));
    };
  };

  open();
  return () => {
    closed = true;
    window.clearTimeout(timer);
    ws?.close();
  };
}
