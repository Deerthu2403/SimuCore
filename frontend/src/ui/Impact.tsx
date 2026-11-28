import { useEffect, useMemo, useState } from "react";
import { ago, clock } from "../twin/format";
import {
  clearSimulation, IMPACT_COLOR, LEVEL, loadImpact, markdownUrl, pct, simulate, useImpact,
  type PropagationEvent, type Report, type ReportRow,
} from "../twin/impact";
import { useTwin } from "../twin/store";
import type { TwinComponent } from "../twin/types";
import { useView } from "../twin/view";
import { useNow } from "./hooks";
import { Icon } from "./Icon";

const open = (id: string) => useView.getState().select({ kind: "component", id }, { fly: true });
const short = (n: string) => n.replace(/ Service$/, "");

/** Failure propagation: what the current (or a simulated) failure may cause, which components matter
 *  most, and how earlier estimates compared with what was then measured. */
export function ImpactPanel() {
  const structure = useTwin((s) => s.mirror?.meta.structure_version);
  const origins = useTwin((s) => s.mirror?.summary.propagation.origins.join());
  useEffect(() => {
    void loadImpact();
    const h = setInterval(() => void loadImpact(), 15000);
    return () => clearInterval(h);
  }, [structure, origins]);
  const error = useImpact((s) => s.error);
  return (
    <div className="impact-panel" data-testid="impact-panel">
      <Current />
      <Simulate />
      <CriticalList />
      <Events />
      {error && <p className="note warn">{error}</p>}
    </div>
  );
}

/** The report of the simulation, or what the twin estimates now. */
function useReport(): { report: Report | null; simulated: boolean } {
  const sim = useImpact((s) => s.simulation);
  const live = useTwin((s) => s.mirror?.summary.propagation);
  const components = useTwin((s) => s.mirror?.components);
  return useMemo(() => {
    if (sim) return { report: sim, simulated: true };
    if (!live || !components || !live.origins.length) return { report: null, simulated: false };
    // the live report is put together from the twin's own impacts
    const name = (id: string) => components[id]?.name ?? id;
    const rows: ReportRow[] = Object.values(components)
      .filter((c) => c.impact && c.impact.role !== "origin")
      .map((c) => ({
        id: c.id, name: c.name, type: c.type, impact: c.impact!.impact, level: c.impact!.level as ReportRow["level"],
        meaning: LEVEL[c.impact!.level].meaning, criticality: c.criticality, score: c.impact!.score,
        steps: c.impact!.steps, chain: c.impact!.chain.map(name), chain_ids: c.impact!.chain,
        origins: c.impact!.origins, status: c.status, confirmed: c.impact!.role === "confirmed",
      }))
      .sort((a, b) => b.score - a.score || b.impact - a.impact);
    return {
      simulated: false,
      report: {
        title: `Impact of ${live.origins.map(name).join(", ")}`, graph: "", components: 0, dependencies: 0, reachable: rows.length,
        origins: live.origins.map((o) => ({ id: o, name: name(o), criticality: components[o]?.criticality ?? 0, status: components[o]?.status ?? null })),
        affected: live.affected, affected_severe: live.affected_severe, affected_any: rows.length,
        expected_affected: live.expected_affected, business_impact: live.business_impact, level: live.level,
        impacts: rows, not_affected_dependencies: [], method: "",
      },
    };
  }, [sim, live, components]);
}

function Current() {
  const { report, simulated } = useReport();
  const since = useTwin((s) => s.mirror?.summary.propagation.since ?? null);
  const now = useNow();
  if (!report) {
    return (
      <section className="wide" aria-label="Current propagation" data-testid="impact-current" data-state="none">
        <h3><Icon name="graph" size={14} color={IMPACT_COLOR} /> Failure propagation</h3>
        <p className="empty">No component has failed: nothing propagates. Simulate a failure to see what it would cause.</p>
      </section>
    );
  }
  return (
    <section className="wide" aria-label="Current propagation" data-testid="impact-current" data-state={simulated ? "simulated" : "live"}>
      <h3>
        <Icon name="graph" size={14} color={IMPACT_COLOR} />
        {simulated ? "Simulation: " : ""}{report.title}
        {simulated
          ? <button type="button" className="linklike" onClick={clearSimulation} data-testid="impact-clear">back to the live twin</button>
          : <span className="muted">since {ago(since, now)}</span>}
      </h3>
      <p className="sub-note">
        {simulated ? "Nothing failed: what would happen, according to the dependency graph. " : "Estimated from the dependency graph; "}
        confirmed = the component's own measurements show trouble, predicted = the graph only.
      </p>
      <div className="impact-figures" data-testid="impact-figures">
        <Figure label="Overall impact" value={LEVEL[report.level].label} />
        <Figure label="Affected (moderate or worse)" value={String(report.affected)} />
        <Figure label="Likely to fail" value={String(report.affected_severe)} />
        <Figure label="Expected number affected" value={report.expected_affected.toFixed(1)} />
        <Figure label="Business impact" value={report.business_impact.toFixed(1)} title="Sum of impact × criticality (1–5)" />
      </div>
      <table className="numbers impact-table" data-testid="impact-table">
        <thead><tr><th>Component</th><th>Impact</th><th>Evidence</th><th>Dependency chain</th></tr></thead>
        <tbody>
          {report.impacts.map((r) => (
            <tr key={r.id} data-id={r.id} data-level={r.level} tabIndex={0} onClick={() => open(r.id)}
              onKeyDown={(e) => { if (e.key === "Enter") open(r.id); }}>
              <th scope="row">{short(r.name)}</th>
              <td><ImpactBar value={r.impact} /> {LEVEL[r.level].label} {pct(r.impact)}</td>
              <td>{simulated ? "simulated" : r.confirmed ? <b>confirmed</b> : "predicted"}</td>
              <td className="chain">{r.chain.map(short).join(" → ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {report.impacts.length === 0 && <p className="empty">Nothing depends on it strongly enough to be affected.</p>}
      {report.not_affected_dependencies.length > 0 && (
        <p className="note" data-testid="impact-unaffected">
          Not affected: {report.not_affected_dependencies.map((x) => short(x.name)).join(", ")}. The failed component
          depends on them, not the other way round; they only lose its calls.
        </p>
      )}
    </section>
  );
}

function Figure({ label, value, title }: { label: string; value: string; title?: string }) {
  return <div title={title}><span className="muted">{label}</span><b>{value}</b></div>;
}

function ImpactBar({ value }: { value: number }) {
  return <span className="ai-bar" role="img" aria-label={`impact ${pct(value)}`}><i style={{ width: `${value * 100}%`, background: IMPACT_COLOR }} /></span>;
}

function Simulate() {
  const components = useTwin((s) => s.mirror?.components);
  const simulating = useImpact((s) => s.simulating);
  const [pick, setPick] = useState("");
  const list = useMemo(() => Object.values(components ?? {}).sort((a, b) => a.name.localeCompare(b.name)), [components]);
  const add = () => {
    if (pick && !simulating.includes(pick)) void simulate([...simulating, pick]);
  };
  return (
    <section aria-label="Simulate a failure" data-testid="impact-simulate">
      <h3>Simulate a failure</h3>
      <p className="sub-note">Nothing is switched off; the twin only calculates.</p>
      <div className="impact-pick">
        <select value={pick} onChange={(e) => setPick(e.target.value)} aria-label="Component to fail" data-testid="impact-select">
          <option value="">Choose a component…</option>
          {list.map((c: TwinComponent) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <button type="button" className="btn text" onClick={add} disabled={!pick} data-testid="impact-run">Fail it</button>
      </div>
      {simulating.length > 0 && (
        <ul className="chips">
          {simulating.map((id) => (
            <li key={id}>
              <button type="button" onClick={() => void simulate(simulating.filter((x) => x !== id))} title="Remove">
                {short(components?.[id]?.name ?? id)} <Icon name="close" size={10} />
              </button>
            </li>
          ))}
          <li><a href={markdownUrl(simulating)} download="impact-report.md" data-testid="impact-download">Report (Markdown)</a></li>
        </ul>
      )}
    </section>
  );
}

function CriticalList() {
  const critical = useImpact((s) => s.critical);
  return (
    <section aria-label="Critical components" data-testid="impact-critical">
      <h3>Critical components</h3>
      <p className="sub-note">Each failing alone, ranked by business impact. Click one to simulate it.</p>
      <table className="numbers ai-table">
        <thead><tr><th>Component</th><th title="Affected, moderate or worse">Aff.</th><th title="Likely to fail">Sev.</th><th title="Sum of impact × criticality">Impact</th></tr></thead>
        <tbody>
          {critical.slice(0, 8).map((c) => (
            <tr key={c.id} data-id={c.id} tabIndex={0} onClick={() => void simulate([c.id])}
              onKeyDown={(e) => { if (e.key === "Enter") void simulate([c.id]); }}>
              <th scope="row">{short(c.name)}{c.single_point && <span className="spof" title="Its failure alone makes at least two others likely to fail"> single point</span>}</th>
              <td>{c.affected}</td><td>{c.severe}</td><td>{c.business_impact.toFixed(1)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Events() {
  const events = useImpact((s) => s.events);
  const source = useImpact((s) => s.source);
  const names = useTwin((s) => s.mirror?.components);
  const name = (id: string) => short(names?.[id]?.name ?? id);
  return (
    <section aria-label="Propagation events" data-testid="impact-events">
      <h3>Propagation events <span className="count">{events.length}</span></h3>
      <p className="sub-note">Predicted, then compared with the measurements of the following 60 s{source === "live" ? " (this run only, no database)" : ""}.</p>
      {events.length === 0 && <p className="empty">No failure has propagated yet.</p>}
      <ul className="ai-history">
        {events.map((e) => <EventRow key={e.id} e={e} name={name} />)}
      </ul>
    </section>
  );
}

function EventRow({ e, name }: { e: PropagationEvent; name: (id: string) => string }) {
  const o = e.outcome;
  const verdict = !e.closed_at ? "open" : !o?.judged ? "not judged (contact lost)"
    : `${o.hits.length} of ${e.predicted.length} predicted came true${o.missed.length ? `, ${o.missed.length} not predicted` : ""}`;
  return (
    <li className="impact-event" data-id={e.id} data-state={e.closed_at ? "closed" : "open"}>
      <time>{clock(e.opened_at)}</time>
      <span className="grow"><b>{e.origins.map(name).join(", ")}</b> · {LEVEL[e.report.level].label}, {e.predicted.length} predicted</span>
      <span className="muted">{verdict}</span>
    </li>
  );
}
