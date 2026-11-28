import { create } from "zustand";
import { neighbours, reach, type Reach } from "./graph";
import { useTwin } from "./store";
import type { ComponentType, Status } from "./types";

/** What the user is looking at. Never holds twin data. */
export type Mode = "normal" | "isolate" | "dependencies";
export type Selection = { kind: "component" | "link"; id: string } | null;
export type CameraRequest = { what: "preset"; name: Preset } | { what: "fit"; ids: string[] } | { what: "step"; name: CameraStep };
export type Preset = "overview" | "top" | "front" | "side";
export type CameraStep = "zoomIn" | "zoomOut" | "left" | "right" | "up" | "down" | "panLeft" | "panRight" | "panUp" | "panDown";
export type Quality = "auto" | "high" | "low";

interface ViewStore {
  selected: Selection;
  hovered: string | null;
  mode: Mode;
  hiddenTypes: ReadonlySet<ComponentType>;
  statusFilter: Status | null;
  structural: boolean;
  labels: boolean;
  quality: Quality;
  searchOpen: boolean;
  left: boolean;
  right: boolean;
  bottom: boolean;
  leftTab: "overview" | "health";
  rightTab: "metrics" | "dependencies" | "events" | "ai";
  bottomTab: "alerts" | "events" | "ai" | "impact";
  /** The rig consumes requests by sequence number. */
  camera: { seq: number; req: CameraRequest };
  select: (s: Selection, opts?: { fly?: boolean }) => void;
  hover: (id: string | null) => void;
  setMode: (m: Mode) => void;
  toggleType: (t: ComponentType) => void;
  setStatusFilter: (s: Status | null) => void;
  request: (r: CameraRequest) => void;
  set: (p: Partial<Pick<ViewStore, "structural" | "labels" | "quality" | "searchOpen" | "left" | "right" | "bottom" | "leftTab" | "rightTab" | "bottomTab">>) => void;
  reset: () => void;
}

export const useView = create<ViewStore>((set, get) => ({
  selected: null,
  hovered: null,
  mode: "normal",
  hiddenTypes: new Set(),
  statusFilter: null,
  structural: false,
  labels: true,
  quality: "auto",
  searchOpen: false,
  left: true,
  right: true,
  bottom: true,
  leftTab: "overview",
  rightTab: "metrics",
  bottomTab: "alerts",
  camera: { seq: 0, req: { what: "preset", name: "overview" } },

  select: (selected, opts) => {
    const old = get().selected;
    if (old?.kind === selected?.kind && old?.id === selected?.id && !opts?.fly) return;
    // A link cannot be the centre of an isolated or dependency view.
    const mode = selected?.kind === "component" ? get().mode : "normal";
    set({ selected, mode });
    if (selected) set({ right: true });
    if (opts?.fly || mode !== "normal") get().request({ what: "fit", ids: idsInView() });
  },
  hover: (hovered) => {
    if (get().hovered !== hovered) set({ hovered });
  },
  setMode: (mode) => {
    if (mode !== "normal" && get().selected?.kind !== "component") return;
    const wasNormal = get().mode === "normal";
    set({ mode });
    if (mode !== "normal") get().request({ what: "fit", ids: idsInView() });
    else if (!wasNormal) get().request({ what: "preset", name: "overview" });
  },
  toggleType: (t) => {
    const next = new Set(get().hiddenTypes);
    if (!next.delete(t)) next.add(t);
    set({ hiddenTypes: next });
  },
  setStatusFilter: (s) => set({ statusFilter: get().statusFilter === s ? null : s }),
  request: (req) => set({ camera: { seq: get().camera.seq + 1, req } }),
  set: (p) => set(p),
  reset: () => {
    set({ selected: null, mode: "normal", hiddenTypes: new Set(), statusFilter: null, hovered: null });
    get().request({ what: "preset", name: "overview" });
  },
}));

/** Which components and links the current mode is about; null = all. */
export function currentReach(): Reach | null {
  const { mode, selected } = useView.getState();
  const links = useTwin.getState().mirror?.links;
  if (mode === "normal" || selected?.kind !== "component" || !links) return null;
  return mode === "isolate" ? neighbours(selected.id, links) : reach(selected.id, links);
}

function idsInView(): string[] {
  const r = currentReach();
  if (r) return [...r.steps.keys()];
  const { selected } = useView.getState();
  if (!selected) return [];
  if (selected.kind === "component") return [selected.id];
  const k = useTwin.getState().mirror?.links[selected.id];
  return k ? [k.source, k.target] : [];
}
