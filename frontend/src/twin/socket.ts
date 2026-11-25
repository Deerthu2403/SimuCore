import type { TwinMessage } from "./types";

export interface SocketHandlers {
  /** Returns false if the message could not be applied; a resync is then requested. */
  onMessage: (m: TwinMessage) => boolean;
  onState: (s: "connecting" | "open" | "closed", retryInMs?: number) => void;
}

/** The twin sends something at least every 5 s; silence for this long means the line is dead. */
export const SILENCE_MS = 12_000;
const MAX_WAIT_MS = 10_000;

/** One-way stream from the twin. It reconnects by itself and only ever asks for a resync. */
export function connectTwin(url: string, h: SocketHandlers): () => void {
  let ws: WebSocket | null = null;
  let attempt = 0;
  let stopped = false;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let silence: ReturnType<typeof setTimeout> | undefined;
  let resyncAsked = false;

  const alive = (sock: WebSocket) => {
    clearTimeout(silence);
    silence = setTimeout(() => lost(sock), SILENCE_MS);
  };

  const lost = (sock: WebSocket) => {
    if (sock !== ws) return;
    ws = null;
    clearTimeout(silence);
    sock.onopen = sock.onmessage = sock.onclose = sock.onerror = null;
    sock.close();
    if (stopped) return;
    const wait = Math.min(500 * 2 ** attempt++, MAX_WAIT_MS) * (0.8 + 0.4 * Math.random());
    h.onState("closed", wait);
    retry = setTimeout(open, wait);
  };

  const open = () => {
    if (stopped) return;
    h.onState("connecting");
    resyncAsked = false;
    const sock = new WebSocket(url);
    ws = sock;
    alive(sock);
    sock.onopen = () => {
      alive(sock);
      h.onState("open");
    };
    sock.onmessage = (ev) => {
      alive(sock);
      let m: TwinMessage;
      try {
        m = JSON.parse(ev.data as string) as TwinMessage;
      } catch {
        lost(sock);
        return;
      }
      if (h.onMessage(m)) {
        attempt = 0;
        if (m.type === "snapshot") resyncAsked = false;
      } else if (!resyncAsked) {
        resyncAsked = true;
        sock.send(JSON.stringify({ type: "resync" }));
      }
    };
    sock.onclose = () => lost(sock);
    sock.onerror = () => lost(sock);
  };

  open();
  return () => {
    stopped = true;
    clearTimeout(retry);
    clearTimeout(silence);
    if (ws) {
      const sock = ws;
      ws = null;
      sock.onopen = sock.onmessage = sock.onclose = sock.onerror = null;
      sock.close();
    }
  };
}
