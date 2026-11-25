import { create } from "zustand";
import { getCatalogue, getTransitions } from "./api";
import { apply, type Mirror, type StatusChange } from "./mirror";
import * as samples from "./samples";
import { connectTwin } from "./socket";
import type { Catalogue, Status, Transition, TwinMessage } from "./types";

export interface HistoryEvent extends Transition {
  key: string;
}

export interface Toast {
  id: number;
  status: Status;
  objectId: string;
  text: string;
  detail: string;
}

/** Per kind: a fault that affects many connections must not push the components out. */
const MAX_EVENTS = 500;

interface TwinStore {
  mirror: Mirror | null;
  link: "connecting" | "open" | "closed";
  /** When the last message arrived (client clock); null before the first. */
  receivedAt: number | null;
  catalogue: Catalogue | null;
  events: HistoryEvent[];
  historyState: "loading" | "ready" | "unavailable";
  toasts: Toast[];
  resyncs: number;
  receive: (m: TwinMessage) => boolean;
  setLink: (s: TwinStore["link"]) => void;
  dismiss: (id: number) => void;
}

/** Called for every change of status, in the same tick as the store changes. */
type ChangeListener = (changes: StatusChange[], structure: boolean) => void;
const changeListeners = new Set<ChangeListener>();
export function onChanges(f: ChangeListener): () => void {
  changeListeners.add(f);
  return () => changeListeners.delete(f);
}

let toastId = 0;

/** History answers and live messages describe the same change differently: several stores may
 *  deliver it, so it is recognised by run, version, object and target status. */
export const eventKey = (t: Transition, run: string) =>
  `${t.run_id ?? run}|${t.version}|${t.object_id}|${t.to_status}`;

export function mergeEvents(old: HistoryEvent[], add: Transition[], run: string): HistoryEvent[] {
  if (!add.length) return old;
  const byKey = new Map(old.map((e) => [e.key, e]));
  for (const t of add) {
    const key = eventKey(t, run);
    byKey.set(key, { ...t, run_id: t.run_id ?? run, key });
  }
  const room = { component: MAX_EVENTS, link: MAX_EVENTS };
  return [...byKey.values()]
    .sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts) || b.version - a.version || (a.key < b.key ? -1 : 1))
    .filter((e) => room[e.object_kind]-- > 0);
}

export const useTwin = create<TwinStore>((set, get) => ({
  mirror: null,
  link: "connecting",
  receivedAt: null,
  catalogue: null,
  events: [],
  historyState: "loading",
  toasts: [],
  resyncs: 0,

  receive: (m) => {
    const prev = get().mirror;
    const r = apply(prev, m);
    if (!r.ok) {
      set({ resyncs: get().resyncs + 1 });
      return false;
    }
    const otherOrigin = !!prev && prev.meta.origin !== r.mirror.meta.origin && prev.meta.origin !== null;
    if (r.restarted || otherOrigin) samples.clear();
    set({
      mirror: r.mirror,
      receivedAt: Date.now(),
      events: mergeEvents(get().events, r.transitions, r.mirror.meta.run_id),
      toasts: [...get().toasts, ...toastsFor(r.changes, r.mirror, prev)].slice(-4),
    });
    samples.record(r.mirror);
    if (r.changes.length || r.structure) changeListeners.forEach((f) => f(r.changes, r.structure));
    // A snapshot carries no transitions: what happened meanwhile is in the history.
    if (m.type === "snapshot") void loadHistory();
    return true;
  },
  setLink: (link) => set({ link }),
  dismiss: (id) => set({ toasts: get().toasts.filter((t) => t.id !== id) }),
}));

function toastsFor(changes: StatusChange[], now: Mirror, prev: Mirror | null): Toast[] {
  if (!prev) return [];
  // When the twin loses or regains contact, everything changes at once; the banner says so.
  if (prev.meta.sync !== now.meta.sync) return [];
  const out: Toast[] = [];
  for (const ch of changes) {
    if (ch.kind !== "component") continue;
    const c = now.components[ch.id];
    const worth = ch.to === "failed" || ch.to === "overloaded"
      || (ch.to === "healthy" && (ch.from === "recovering" || ch.from === "overloaded" || ch.from === "failed"));
    if (!worth) continue;
    const what = ch.to === "failed" ? "has failed" : ch.to === "overloaded" ? "is overloaded" : "is healthy again";
    out.push({ id: ++toastId, status: ch.to, objectId: ch.id, text: `${c.name} ${what}`, detail: c.reason });
  }
  return out;
}

let historyLoading = false;
export async function loadHistory(): Promise<void> {
  if (historyLoading) return;
  historyLoading = true;
  try {
    const rows = (await Promise.all(
      (["component", "link"] as const).map((k) => getTransitions(k, 120, MAX_EVENTS)))).flat();
    const run = useTwin.getState().mirror?.meta.run_id ?? "";
    useTwin.setState((s) => ({ events: mergeEvents(s.events, rows, run), historyState: "ready" }));
  } catch {
    useTwin.setState({ historyState: "unavailable" });
  } finally {
    historyLoading = false;
  }
}

async function loadCatalogue(): Promise<void> {
  try {
    useTwin.setState({ catalogue: await getCatalogue() });
  } catch {
    /* tried again with the next connection */
  }
}

/** Connects this page to the twin. Returns a function that disconnects it. */
export function startTwin(): () => void {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return connectTwin(`${proto}://${location.host}/ws/v2/twin`, {
    onMessage: (m) => useTwin.getState().receive(m),
    onState: (s) => {
      useTwin.getState().setLink(s);
      if (s === "open" && !useTwin.getState().catalogue) void loadCatalogue();
    },
  });
}

/** Is what the page shows current? */
export type Freshness = "live" | "stale" | "waiting" | "offline" | "connecting";
export function freshness(s: Pick<TwinStore, "mirror" | "link">): Freshness {
  if (!s.mirror) return s.link === "closed" ? "offline" : "connecting";
  if (s.link !== "open") return "offline";
  return s.mirror.meta.sync;
}

const NO_UNITS: Record<string, string> = {};
export const useUnits = () => useTwin((s) => s.catalogue?.metric_units ?? NO_UNITS);
export const useComponent = (id: string | null) =>
  useTwin((s) => (id ? s.mirror?.components[id] : undefined));
export const useLink = (id: string | null) => useTwin((s) => (id ? s.mirror?.links[id] : undefined));
export const useFreshness = () => useTwin(freshness);
