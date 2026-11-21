import type { TwinMessage } from "../types/twin";
import { useTwinStore } from "../store/twinStore";

/** Connects to /ws/twin, feeds the store, reconnects with exponential back-off. */
export function connectTwinSocket(): () => void {
  let ws: WebSocket | null = null;
  let retry = 0;
  let closed = false;
  let timer: number | undefined;
  const store = useTwinStore.getState;

  const open = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws/twin`);
    store().setConnection("connecting");

    ws.onopen = () => {
      retry = 0;
      store().setConnection("live");
    };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data) as TwinMessage;
      if (msg.type === "snapshot") store().setSnapshot(msg.topology, msg.states, msg.version);
      else if (msg.type === "delta") store().applyDelta(msg.states, msg.version);
    };
    ws.onclose = () => {
      store().setConnection("offline");
      if (closed) return;
      const delay = Math.min(1000 * 2 ** retry++, 15000);
      timer = window.setTimeout(open, delay);
    };
  };

  open();
  return () => {
    closed = true;
    window.clearTimeout(timer);
    ws?.close();
  };
}
