import { create } from "zustand";
import { useShallow } from "zustand/react/shallow";
import type { HealthStatus, NodeLayer, NodeState, PropagationResponse, Topology } from "../types/twin";

export type Connection = "connecting" | "live" | "offline";

interface TwinStore {
  connection: Connection;
  version: number;
  topology: Topology | null;
  states: Record<string, NodeState>;
  selectedId: string | null;
  impact: PropagationResponse | null;
  hiddenLayers: Set<NodeLayer>;

  setConnection: (c: Connection) => void;
  setSnapshot: (t: Topology, s: NodeState[], version: number) => void;
  applyDelta: (s: NodeState[], version: number) => void;
  select: (id: string | null) => void;
  setImpact: (r: PropagationResponse | null) => void;
  toggleLayer: (l: NodeLayer) => void;
}

const index = (s: NodeState[]) => Object.fromEntries(s.map((x) => [x.node_id, x]));

export const useTwinStore = create<TwinStore>((set) => ({
  connection: "connecting",
  version: 0,
  topology: null,
  states: {},
  selectedId: null,
  impact: null,
  hiddenLayers: new Set(),

  setConnection: (connection) => set({ connection }),
  setSnapshot: (topology, states, version) => set({ topology, states: index(states), version }),
  applyDelta: (delta, version) =>
    set((st) => ({ states: { ...st.states, ...index(delta) }, version })),
  select: (selectedId) => set({ selectedId }),
  setImpact: (impact) => set({ impact }),
  toggleLayer: (l) =>
    set((st) => {
      const hiddenLayers = new Set(st.hiddenLayers);
      hiddenLayers.has(l) ? hiddenLayers.delete(l) : hiddenLayers.add(l);
      return { hiddenLayers };
    }),
}));

export const STATUS_COLORS: Record<HealthStatus, string> = {
  healthy: "#22c55e",
  degraded: "#f59e0b",
  critical: "#ef4444",
  down: "#991b1b",
  unknown: "#64748b",
};

export function useStatusSummary(): Record<HealthStatus, number> {
  return useTwinStore(
    useShallow((s) => {
      const out: Record<HealthStatus, number> = { healthy: 0, degraded: 0, critical: 0, down: 0, unknown: 0 };
      for (const st of Object.values(s.states)) out[st.status]++;
      return out;
    }),
  );
}
