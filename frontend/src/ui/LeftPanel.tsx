import { useMemo } from "react";
import { bySeverity, STATUS, text, TYPE, TYPE_ORDER } from "../twin/format";
import { iconOf } from "../twin/icons";
import { useTwin, useUnits } from "../twin/store";
import type { ComponentType, Status, TwinComponent } from "../twin/types";
import { useView } from "../twin/view";
import { Icon, StatusChip, StatusDot } from "./Icon";
import { Tabs } from "./Inspector";

export function LeftPanel() {
  const tab = useView((s) => s.leftTab);
  const set = useView((s) => s.set);
  return (
    <aside className="panel left" aria-label="Infrastructure" data-testid="left-panel">
      <Tabs value={tab} onChange={(t) => set({ leftTab: t })} items={[["overview", "Infrastructure"], ["health", "Service health"]]} />
      <div className="scroll">{tab === "overview" ? <Overview /> : <Health />}</div>
    </aside>
  );
}

function Overview() {
  const components = useTwin((s) => s.mirror?.components);
  const summary = useTwin((s) => s.mirror?.summary);
  const hidden = useView((s) => s.hiddenTypes);
  const toggle = useView((s) => s.toggleType);
  const groups = useMemo(() => TYPE_ORDER
    .map((t): [ComponentType, TwinComponent[]] => [t, Object.values(components ?? {}).filter((c) => c.type === t)])
    .filter(([, list]) => list.length), [components]);
  if (!components || !summary) return <p className="empty">Waiting for the digital twin…</p>;
  return (
    <>
      <section className="summary" data-testid="summary">
        <div><span className="muted">Bank</span><StatusChip status={summary.overall} /></div>
        <div><span className="muted">Components</span><b>{summary.components}</b></div>
        <div><span className="muted">Connections</span><b>{summary.links}</b></div>
        <div><span className="muted">Failed connections</span><b>{summary.links_failed}</b></div>
      </section>
      {groups.map(([type, list]) => {
        const off = hidden.has(type);
        const worst = list.reduce<Status>((a, c) => (bySeverity(a, c.status) > 0 ? c.status : a), "healthy");
        return (
          <section key={type} className={`group ${off ? "off" : ""}`} data-type={type}>
            <h3>
              <StatusDot status={worst} /> {TYPE[type].plural} <span className="count">{list.length}</span>
              <button type="button" className="eye" onClick={() => toggle(type)} aria-pressed={!off} title={off ? "Show in the scene" : "Hide in the scene"} aria-label={`${off ? "Show" : "Hide"} ${TYPE[type].plural}`}>
                <Icon name={off ? "eyeOff" : "eye"} size={14} />
              </button>
            </h3>
            <ul>{list.map((c) => <Row key={c.id} c={c} />)}</ul>
          </section>
        );
      })}
    </>
  );
}

function Row({ c }: { c: TwinComponent }) {
  const selected = useView((s) => s.selected?.kind === "component" && s.selected.id === c.id);
  const hovered = useView((s) => s.hovered === c.id);
  const { select, hover } = useView.getState();
  const units = useUnits();
  const key = ["ingress_rps", "rps", "calls_per_s", "cpu_pct", "hosted_cpu_pct", "db_pool_in_use"].find((k) => k in c.metrics);
  return (
    <li>
      <button
        type="button" className={`row-btn ${selected ? "selected" : ""} ${hovered ? "hovered" : ""}`} data-id={c.id} data-status={c.status}
        onMouseEnter={() => hover(c.id)} onMouseLeave={() => hover(null)}
        onClick={() => select({ kind: "component", id: c.id }, { fly: true })}
      >
        <StatusDot status={c.status} />
        <Icon name={iconOf(c)} size={14} />
        <span className="grow">{c.name}</span>
        {key && <span className="num">{text(key, c.metrics[key], units)}</span>}
      </button>
    </li>
  );
}

const COLUMNS = [["cpu_pct", "CPU"], ["mem_pct", "RAM"], ["latency_ms", "p95"], ["rps", "req/s"], ["error_rate", "Errors"]] as const;

function Health() {
  const components = useTwin((s) => s.mirror?.components);
  const units = useUnits();
  const selected = useView((s) => s.selected?.id);
  const { select, hover } = useView.getState();
  const list = useMemo(() => Object.values(components ?? {}).filter((c) => c.type === "microservice")
    .sort((a, b) => bySeverity(a.status, b.status) || a.name.localeCompare(b.name)), [components]);
  if (!list.length) return <p className="empty">Waiting for the digital twin…</p>;
  const cell = (k: string, v: number | undefined) =>
    k === "rps" && v !== undefined ? text(k, v, {}) : text(k, v, units).replace(" ms", "").replace(" %", "");
  return (
    <div className="health" data-testid="health">
      <table className="numbers">
        <thead><tr><th>Service</th>{COLUMNS.map(([k, l]) => <th key={k} title={k === "latency_ms" ? "95th percentile in ms" : undefined}>{l}</th>)}</tr></thead>
        <tbody>
          {list.map((c) => (
            <tr key={c.id} data-id={c.id} className={selected === c.id ? "selected" : ""} tabIndex={0}
              onClick={() => select({ kind: "component", id: c.id }, { fly: true })}
              onKeyDown={(e) => { if (e.key === "Enter") select({ kind: "component", id: c.id }, { fly: true }); }}
              onMouseEnter={() => hover(c.id)} onMouseLeave={() => hover(null)}>
              <th scope="row"><Icon name={STATUS[c.status].icon} size={12} color={STATUS[c.status].color} title={STATUS[c.status].label} /> {c.name.replace(/ Service$/, "")}</th>
              {COLUMNS.map(([k]) => <td key={k} data-metric={k}>{cell(k, c.metrics[k])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="note">CPU, RAM and errors in %, p95 in ms. Most serious first.</p>
    </div>
  );
}
