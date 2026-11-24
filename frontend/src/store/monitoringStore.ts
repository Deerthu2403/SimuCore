import { create } from "zustand";

/** Mirrors monitoring/monitor/quality.py (Event.to_dict). */
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

export interface Overview {
  services: number;
  up: number;
  down: number;
  unresponsive: number;
  total_rps: number;
  error_rate: number | null;
  total_net_bps: number;
}

export interface MonitoringMessage {
  type: "snapshot" | "update";
  fresh: boolean;
  origin: "measured" | "synthetic" | null;
  overview: Overview;
  services: { service: string; node_id: string; status: string }[];
  /** Snapshot: all open events. Update: only those that opened or closed. */
  events: MonitoringEvent[];
}

interface MonitoringStore {
  connected: boolean;
  fresh: boolean;
  origin: string | null;
  overview: Overview | null;
  nodeOf: Record<string, string>;
  open: Record<string, MonitoringEvent>;
  setConnected: (c: boolean) => void;
  receive: (m: MonitoringMessage) => void;
}

export const useMonitoringStore = create<MonitoringStore>((set) => ({
  connected: false,
  fresh: false,
  origin: null,
  overview: null,
  nodeOf: {},
  open: {},
  setConnected: (connected) => set({ connected }),
  receive: (m) =>
    set((st) => {
      const open = m.type === "snapshot" ? {} : { ...st.open };
      for (const e of m.events) {
        if (e.closed_at) delete open[e.id];
        else open[e.id] = e;
      }
      return {
        fresh: m.fresh,
        origin: m.origin,
        overview: m.overview,
        nodeOf: Object.fromEntries(m.services.map((s) => [s.service, s.node_id])),
        open,
      };
    }),
}));
