import type { Meta, Status, Summary, Transition, TwinComponent, TwinLink, TwinMessage } from "./types";

/** The client's copy of the twin. Immutable: unchanged objects keep their identity between messages,
 *  so that React re-renders only what changed. */
export interface Mirror {
  meta: Meta;
  summary: Summary;
  components: Readonly<Record<string, TwinComponent>>;
  links: Readonly<Record<string, TwinLink>>;
  /** Stable order; new arrays only when the structure changes. */
  componentIds: readonly string[];
  linkIds: readonly string[];
}

export interface StatusChange {
  kind: "component" | "link";
  id: string;
  from: Status;
  to: Status;
}

export type Applied =
  | { ok: true; mirror: Mirror; changes: StatusChange[]; transitions: Transition[]; structure: boolean; restarted: boolean }
  | { ok: false; why: string };

const sameIds = (a: readonly string[], b: readonly string[]) =>
  a.length === b.length && a.every((x, i) => x === b[i]);

/** Applies one message. ``ok: false`` means the copy cannot be trusted: ask for a resync. */
export function apply(prev: Mirror | null, m: TwinMessage): Applied {
  if (m.type === "snapshot") return snapshot(prev, m);
  if (!prev) return { ok: false, why: "no snapshot yet" };
  if (m.meta.run_id !== prev.meta.run_id) return { ok: false, why: "the twin restarted" };
  if (m.base_version !== prev.meta.version) {
    return { ok: false, why: `expected version ${prev.meta.version}, message builds on ${m.base_version}` };
  }
  if (m.meta.structure_version !== prev.meta.structure_version) {
    return { ok: false, why: "the structure changed" };
  }
  if (m.type === "heartbeat") {
    if (m.meta.version !== prev.meta.version) return { ok: false, why: "missed a change" };
    return done({ ...prev, meta: m.meta }, [], [], false, false);
  }
  const changes: StatusChange[] = [];
  let { components, links } = prev;
  if (m.components.length) {
    const next: Record<string, TwinComponent> = { ...components };
    for (const c of m.components) {
      const old = next[c.id];
      if (!old) return { ok: false, why: `unknown component ${c.id}` };
      if (old.status !== c.status) changes.push({ kind: "component", id: c.id, from: old.status, to: c.status });
      next[c.id] = c;
    }
    components = next;
  }
  if (m.links.length) {
    const next: Record<string, TwinLink> = { ...links };
    for (const k of m.links) {
      const old = next[k.id];
      if (!old) return { ok: false, why: `unknown link ${k.id}` };
      if (old.status !== k.status) changes.push({ kind: "link", id: k.id, from: old.status, to: k.status });
      next[k.id] = k;
    }
    links = next;
  }
  return done(
    { ...prev, meta: m.meta, summary: m.summary ?? prev.summary, components, links },
    changes, m.transitions, false, false,
  );
}

function snapshot(prev: Mirror | null, m: TwinMessage): Applied {
  if (!m.summary) return { ok: false, why: "snapshot without summary" };
  const components = Object.fromEntries(m.components.map((c) => [c.id, c]));
  const links = Object.fromEntries(
    m.links.filter((k) => k.source in components && k.target in components).map((k) => [k.id, k]),
  );
  const componentIds = m.components.map((c) => c.id);
  const linkIds = m.links.filter((k) => k.id in links).map((k) => k.id);
  const changes: StatusChange[] = [];
  const restarted = !!prev && prev.meta.run_id !== m.meta.run_id;
  if (prev) {
    for (const c of m.components) {
      const old = prev.components[c.id];
      if (old && old.status !== c.status) changes.push({ kind: "component", id: c.id, from: old.status, to: c.status });
    }
    for (const k of m.links) {
      const old = prev.links[k.id];
      if (old && old.status !== k.status) changes.push({ kind: "link", id: k.id, from: old.status, to: k.status });
    }
  }
  const sameC = !!prev && sameIds(prev.componentIds, componentIds);
  const sameL = !!prev && sameIds(prev.linkIds, linkIds);
  return done(
    {
      meta: m.meta, summary: m.summary, components, links,
      componentIds: sameC ? prev!.componentIds : componentIds,
      linkIds: sameL ? prev!.linkIds : linkIds,
    },
    changes, m.transitions, !(sameC && sameL), restarted,
  );
}

function done(
  mirror: Mirror, changes: StatusChange[], transitions: Transition[], structure: boolean, restarted: boolean,
): Applied {
  return { ok: true, mirror, changes, transitions, structure, restarted };
}
