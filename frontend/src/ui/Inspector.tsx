import { useEffect, useMemo, useState } from "react";
import { getServiceHistory } from "../twin/api";
import { ago, clock, formatMetric, KEY_METRICS, KIND, metricLabel, STATUS, text, TYPE } from "../twin/format";
import { iconOf } from "../twin/icons";
import * as samples from "../twin/samples";
import { useComponent, useFreshness, useLink, useTwin, useUnits } from "../twin/store";
import type { DependencyRef, TwinComponent, TwinLink } from "../twin/types";
import { useView } from "../twin/view";
import { AiTab } from "./Ai";
import { describe, IMPACT_COLOR, impactOf, LEVEL, ROLE, useImpact } from "../twin/impact";
import { useNow } from "./hooks";
import { DependencyGraph } from "./DependencyGraph";
import { EventList } from "./Events";
import { Button, Icon, StatusChip, StatusDot } from "./Icon";
import { Sparkline, useStats } from "./Sparkline";

export function Inspector() {
  const selected = useView((s) => s.selected);
  const close = useView((s) => s.select);
  return (
    <aside className="panel right" aria-label="Inspector" data-testid="inspector">
      {selected?.kind === "component" ? <ComponentInspector id={selected.id} key={selected.id} />
        : selected?.kind === "link" ? <LinkInspector id={selected.id} key={selected.id} close={() => close(null)} />
        : <LiveOverview />}
    </aside>
  );
}

function ComponentInspector({ id }: { id: string }) {
  const c = useComponent(id);
  const tab = useView((s) => s.rightTab);
  const mode = useView((s) => s.mode);
  const { set, setMode, select, request } = useView.getState();
  if (!c) return <Gone />;
  const toggle = (m: "isolate" | "dependencies") => setMode(mode === m ? "normal" : m);
  return (
    <>
      <header className="inspector-head">
        <div className="title">
          <Icon name={iconOf(c)} size={20} />
          <div>
            <h2 data-testid="inspector-name">{c.name}</h2>
            <span className="muted">{TYPE[c.type].label} · <code>{c.id}</code></span>
          </div>
          <Button icon="close" label="Close" kbd="Esc" onClick={() => { setMode("normal"); select(null); }} />
        </div>
        <StatusLine c={c} />
        <div className="actions">
          <Button icon="focus" label="Focus" kbd="F" text onClick={() => request({ what: "fit", ids: [id] })} />
          <Button icon="isolate" label="Isolate" kbd="I" text active={mode === "isolate"} onClick={() => toggle("isolate")} />
          <Button icon="graph" label="Dependencies" kbd="D" text active={mode === "dependencies"} onClick={() => { toggle("dependencies"); set({ rightTab: "dependencies" }); }} />
        </div>
      </header>
      <Tabs value={tab} onChange={(t) => set({ rightTab: t })} items={[["metrics", "Live metrics"], ["dependencies", "Dependencies"], ["events", "Events"], ["ai", "AI"]]} />
      <div className="scroll">
        {tab === "metrics" && <MetricsTab c={c} />}
        {tab === "dependencies" && <DependenciesTab c={c} />}
        {tab === "events" && <EventList objectId={id} />}
        {tab === "ai" && <AiTab c={c} />}
      </div>
    </>
  );
}

function StatusLine({ c }: { c: TwinComponent }) {
  const now = useNow();
  const names = useTwin((s) => s.mirror?.components);
  const causes = c.root_causes.filter((r) => r !== c.id);
  const { select } = useView.getState();
  return (
    <div className="status-line">
      <div className="row">
        <span data-testid="inspector-status"><StatusChip status={c.status} /></span>
        <span className="muted">for {ago(c.status_since, now)}{c.previous_status ? `, before: ${STATUS[c.previous_status].label.toLowerCase()}` : ""}</span>
      </div>
      {c.reason && <p className="reason">{c.reason}</p>}
      {causes.length > 0 && (
        <p className="causes">Root cause:{" "}
          {causes.map((r) => (
            <button type="button" className="linklike" key={r} onClick={() => select({ kind: "component", id: r }, { fly: true })}>{names?.[r]?.name ?? r}</button>
          ))}
        </p>
      )}
      <ImpactBox c={c} />
    </div>
  );
}

/** How a failure reaches this component: the dependency chain, the estimated impact, the evidence. */
function ImpactBox({ c }: { c: TwinComponent }) {
  const sim = useImpact((s) => s.simulation);
  const names = useTwin((s) => s.mirror?.components);
  const i = impactOf(c, sim);
  const { select } = useView.getState();
  if (!i || !names) return null;
  const name = (x: string) => names[x]?.name.replace(/ Service$/, "") ?? x;
  return (
    <div className="impact-box" data-role={i.role} data-testid="inspector-impact">
      <b style={{ color: IMPACT_COLOR }}>{sim ? "Simulated " : ""}{i.role === "origin" ? (sim ? "failure" : "Failure origin") : ROLE[i.role].label}</b>
      {i.role !== "origin" && <> · {describe(i, (x) => names[x]?.name ?? x)}</>}
      <div className="muted">{i.role === "origin"
        ? `its failure may spread to ${sim ? sim.affected : "the components that depend on it"}`
        : `${ROLE[i.role].meaning}; ${LEVEL[i.level].meaning}`}</div>
      {i.chain.length > 1 && (
        <div className="chain" data-testid="impact-chain">
          {i.chain.map((x, n) => (
            <span key={x}>
              {n > 0 && "→ "}
              <button type="button" className="linklike" onClick={() => select({ kind: "component", id: x }, { fly: true })}>{name(x)}</button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------- metrics
const HISTORY = ["cpu_pct", "mem_pct", "latency_ms", "rps", "error_rate", "queue_length", "in_flight", "db_response_ms"];

function usePrefill(c: TwinComponent): void {
  const service = c.type === "microservice" ? String(c.attributes.service ?? "") : "";
  const origin = useTwin((s) => s.mirror?.meta.origin);
  useEffect(() => {
    if (!service || !origin) return;
    const stop = new AbortController();
    getServiceHistory(service, HISTORY, origin, 6, stop.signal)
      .then((h) => h.forEach((points, m) => samples.prefill(c.id, m, points)))
      .catch(() => undefined); // without the database the curve simply starts now
    return () => stop.abort();
  }, [c.id, service, origin]);
}

function MetricsTab({ c }: { c: TwinComponent }) {
  usePrefill(c);
  const fresh = useFreshness();
  const keys = KEY_METRICS.filter((k) => k in c.metrics);
  const others = Object.keys(c.metrics).filter((k) => !KEY_METRICS.includes(k)).sort();
  const missing = c.type === "microservice" ? ["cpu_pct", "mem_pct", "latency_ms", "rps"].filter((k) => !(k in c.metrics)) : [];
  return (
    <>
      {c.data_quality === "derived" && <p className="note">Derived: {c.description}</p>}
      {c.data_quality === "partial" && <p className="note">Some of its measurements are missing.</p>}
      {fresh !== "live" && <p className="note warn">These values are not current.</p>}
      {c.findings.length > 0 && (
        <ul className="findings">
          {c.findings.map((f) => (
            <li key={f.rule + f.metric}>
              <Icon name={f.level === "failure" ? "cross" : f.level === "overload" ? "gauge" : "triangle"} size={13}
                color={STATUS[f.level === "failure" ? "failed" : f.level === "overload" ? "overloaded" : "warning"].color} />
              {f.message}
            </li>
          ))}
        </ul>
      )}
      {keys.length === 0 && others.length === 0 && <p className="empty">Nothing is measured here at the moment.</p>}
      <div className="tiles">
        {[...keys, ...others].slice(0, 8).map((k) => <Tile key={k} id={c.id} metric={k} value={c.metrics[k]} max={k === "in_flight" ? num(c.attributes.max_concurrency) : undefined} />)}
        {missing.map((k) => (
          <div className="tile" key={k} data-metric={k}><span className="tile-label">{metricLabel(k)}</span><span className="tile-value none">not measured</span></div>
        ))}
      </div>
      {(keys.length > 0 || others.length > 0) && <MetricTable id={c.id} metrics={c.metrics} />}
      <Facts c={c} />
    </>
  );
}

const num = (v: unknown) => (typeof v === "number" ? v : undefined);

function Tile({ id, metric, value, max }: { id: string; metric: string; value: number; max?: number }) {
  const units = useUnits();
  const [v, unit] = formatMetric(metric, value, units);
  const share = units[metric] === "%" ? value / 100 : max ? value / max : null;
  return (
    <div className="tile" data-metric={metric} data-value={value}>
      <span className="tile-label">{metricLabel(metric)}</span>
      <span className="tile-value">{v}<small>{max ? `/ ${max}` : unit}</small></span>
      {share !== null && <span className="meter" role="presentation"><i style={{ width: `${Math.min(100, Math.max(0, share * 100))}%` }} /></span>}
      <Sparkline id={id} metric={metric} units={units} label={metricLabel(metric)} />
    </div>
  );
}

function MetricTable({ id, metrics }: { id: string; metrics: TwinComponent["metrics"] }) {
  const [open, setOpen] = useState(false);
  const keys = Object.keys(metrics).sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  return (
    <section>
      <button type="button" className="fold" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "Hide" : "Show"} {keys.length === 1 ? "the measurement" : `all ${keys.length} measurements`} as a table
      </button>
      {open && (
        <table className="numbers">
          <thead><tr><th>Measurement</th><th>Now</th><th>Min</th><th>Mean</th><th>Max</th></tr></thead>
          <tbody>{keys.map((k) => <MetricRow key={k} id={id} metric={k} value={metrics[k]} />)}</tbody>
        </table>
      )}
    </section>
  );
}

const rank = (k: string) => (KEY_METRICS.includes(k) ? KEY_METRICS.indexOf(k) : 99);

function MetricRow({ id, metric, value }: { id: string; metric: string; value: number }) {
  const units = useUnits();
  const s = useStats(id, metric);
  const f = (v: number | undefined) => text(metric, v, units);
  return (
    <tr><th scope="row">{metricLabel(metric)}</th><td>{f(value)}</td><td>{f(s?.min)}</td><td>{f(s?.mean)}</td><td>{f(s?.max)}</td></tr>
  );
}

function Facts({ c }: { c: TwinComponent }) {
  const rows: [string, string][] = [
    ["Importance", `${c.criticality} of 5`],
    ["Data quality", c.data_quality],
    ...Object.entries(c.attributes).filter(([, v]) => v !== null && v !== undefined)
      .map(([k, v]): [string, string] => [k.replace(/_/g, " "), k === "baseline_latency_ms" ? `${v} ms` : String(v)]),
  ];
  return (
    <dl className="facts">
      {rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
    </dl>
  );
}

// -------------------------------------------------------------- dependencies
function DependenciesTab({ c }: { c: TwinComponent }) {
  return (
    <>
      <DependencyGraph center={c.id} />
      <RefList title="Depends on" refs={c.dependencies.depends_on} empty="It depends on nothing." />
      <RefList title="Needed by" refs={c.dependencies.dependents} empty="Nothing depends on it." />
    </>
  );
}

function RefList({ title, refs, empty }: { title: string; refs: DependencyRef[]; empty: string }) {
  const sorted = useMemo(() => [...refs].sort((a, b) => STATUS[b.status].rank - STATUS[a.status].rank || a.kind.localeCompare(b.kind)), [refs]);
  return (
    <section className="reflist" aria-label={title}>
      <h3>{title} <span className="count">{refs.length}</span></h3>
      {refs.length === 0 && <p className="empty">{empty}</p>}
      <ul>{sorted.map((r) => <RefRow key={r.link_id} r={r} />)}</ul>
    </section>
  );
}

function RefRow({ r }: { r: DependencyRef }) {
  const c = useComponent(r.id);
  const k = useLink(r.link_id);
  const units = useUnits();
  const { select, hover } = useView.getState();
  if (!c) return null;
  const m = k?.metrics ?? {};
  return (
    <li>
      <button
        type="button" className="row-btn" data-ref={r.id} onMouseEnter={() => hover(r.id)} onMouseLeave={() => hover(null)}
        onClick={() => select({ kind: "component", id: r.id }, { fly: true })}
      >
        <StatusDot status={c.status} />
        <span className="grow">{c.name}</span>
        <span className="muted">{KIND[r.kind]}{r.mode !== "structural" ? `, ${r.mode.replace("_", " ")}` : ""}</span>
      </button>
      {k && "calls_per_s" in m && (
        <button type="button" className="sub" onClick={() => select({ kind: "link", id: r.link_id }, { fly: true })} title="Show this connection">
          <StatusDot status={k.status} />
          {text("calls_per_s", m.calls_per_s, units)}
          {"latency_ms" in m && ` · ${text("latency_ms", m.latency_ms, units)}`}
          {m.failure_rate > 0 && ` · ${text("failure_rate", m.failure_rate, units)} fail`}
          {m.circuit > 0 && ` · circuit ${text("circuit", m.circuit, units)}`}
        </button>
      )}
    </li>
  );
}

// ---------------------------------------------------------------------- link
function LinkInspector({ id, close }: { id: string; close: () => void }) {
  const k = useLink(id);
  const a = useComponent(k?.source ?? null);
  const b = useComponent(k?.target ?? null);
  const now = useNow();
  const { select, request } = useView.getState();
  if (!k || !a || !b) return <Gone />;
  const metrics = KEY_METRICS.filter((m) => m in k.metrics);
  const end = (c: TwinComponent) => (
    <button type="button" className="row-btn" onClick={() => select({ kind: "component", id: c.id }, { fly: true })}>
      <StatusDot status={c.status} /><span className="grow">{c.name}</span>
    </button>
  );
  return (
    <>
      <header className="inspector-head">
        <div className="title">
          <Icon name="link" size={20} />
          <div><h2 data-testid="inspector-name">{a.name} → {b.name}</h2><span className="muted">Connection · {KIND[k.kind]} · {k.mode.replace("_", " ")}</span></div>
          <Button icon="close" label="Close" kbd="Esc" onClick={close} />
        </div>
        <div className="status-line">
          <div className="row"><span data-testid="inspector-status"><StatusChip status={k.status} /></span><span className="muted">for {ago(k.status_since, now)}</span></div>
          {k.reason && <p className="reason">{k.reason}</p>}
        </div>
        <div className="actions"><Button icon="focus" label="Focus" kbd="F" text onClick={() => request({ what: "fit", ids: [k.source, k.target] })} /></div>
      </header>
      <div className="scroll">
        {k.purpose && <p className="note">{k.purpose}{k.data_quality === "derived" ? " (derived from the components at its ends)" : ""}</p>}
        <div className="tiles">{metrics.map((m) => <Tile key={m} id={k.id} metric={m} value={k.metrics[m]} />)}</div>
        {Object.keys(k.metrics).length === 0 && <p className="empty">No traffic is measured on this connection.</p>}
        {Object.keys(k.metrics).length > 0 && <MetricTable id={k.id} metrics={k.metrics} />}
        <section className="reflist"><h3>From</h3>{end(a)}<h3>To</h3>{end(b)}</section>
      </div>
    </>
  );
}

// ------------------------------------------------------------ nothing selected
/** Live totals of the whole bank, each read from the component that has it. */
function LiveOverview() {
  const list = useTwin((s) => s.mirror?.components);
  const meta = useTwin((s) => s.mirror?.meta);
  const links = useTwin((s) => s.mirror?.links);
  const units = useUnits();
  const entry = useMemo(() => Object.values(list ?? {}).find((c) => c.type === "load_balancer"), [list]);
  const net = useMemo(() => Object.values(list ?? {}).find((c) => c.type === "network"), [list]);
  const busiest = useMemo(() => Object.values(links ?? {}).filter((k) => k.active)
    .sort((a, b) => (b.metrics.calls_per_s ?? 0) - (a.metrics.calls_per_s ?? 0)).slice(0, 5), [links]);
  const { select } = useView.getState();
  if (!list || !meta) return <p className="empty">Waiting for the digital twin…</p>;
  const tile = (c: TwinComponent | undefined, m: string) => c && m in c.metrics && <Tile key={c.id + m} id={c.id} metric={m} value={c.metrics[m]} />;
  return (
    <>
      <header className="inspector-head"><div className="title"><Icon name="gauge" size={20} /><div><h2>Live metrics</h2>
        <span className="muted">whole bank · measured {meta.last_observation_at ? clock(meta.last_observation_at) : "–"}</span></div></div></header>
      <div className="scroll">
        <div className="tiles">
          {tile(entry, "ingress_rps")}{tile(entry, "error_rate")}{tile(net, "calls_per_s")}{tile(net, "failure_rate")}
          {tile(net, "bytes_per_s")}{tile(net, "connections")}
        </div>
        <section className="reflist">
          <h3>Busiest connections</h3>
          {busiest.length === 0 && <p className="empty">No requests at the moment.</p>}
          <ul>{busiest.map((k) => <Busy key={k.id} k={k} units={units} names={list} onClick={() => select({ kind: "link", id: k.id }, { fly: true })} />)}</ul>
        </section>
        <p className="note">Select a component in the scene, in a list or with the search to see its measurements.</p>
      </div>
    </>
  );
}

function Busy({ k, names, units, onClick }: { k: TwinLink; names: Record<string, TwinComponent>; units: Record<string, string>; onClick: () => void }) {
  return (
    <li><button type="button" className="row-btn" onClick={onClick}>
      <StatusDot status={k.status} />
      <span className="grow">{names[k.source]?.name.replace(/ Service$/, "")} → {names[k.target]?.name.replace(/ Service$/, "")}</span>
      <span className="num">{text("calls_per_s", k.metrics.calls_per_s, units)}</span>
    </button></li>
  );
}

function Gone() {
  return <p className="empty">This object is no longer part of the infrastructure.</p>;
}

export function Tabs<T extends string>({ value, onChange, items }: { value: T; onChange: (t: T) => void; items: [T, React.ReactNode][] }) {
  return (
    <div className="tabs" role="tablist">
      {items.map(([k, label]) => (
        <button type="button" key={k} role="tab" aria-selected={value === k} className={value === k ? "active" : ""} onClick={() => onChange(k)}>{label}</button>
      ))}
    </div>
  );
}
