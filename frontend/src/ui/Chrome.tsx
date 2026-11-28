import { useEffect, useMemo, useRef, useState } from "react";
import { search } from "../twin/graph";
import { ago, clock, STATUS, TYPE } from "../twin/format";
import { iconOf } from "../twin/icons";
import { useFreshness, useTwin, type Toast } from "../twin/store";
import { useImpact } from "../twin/impact";
import { STATUSES, type Status } from "../twin/types";
import { useView, type Preset } from "../twin/view";
import { Alerts, EventList, useAlerts } from "./Events";
import { AI_COLOR } from "../twin/ai";
import { AiPanel } from "./Ai";
import { ImpactPanel } from "./Impact";
import { IMPACT_COLOR } from "../twin/impact";
import { useNow } from "./hooks";
import { Button, Icon, StatusChip } from "./Icon";
import { Tabs } from "./Inspector";

// -------------------------------------------------------------------- top bar
const FRESH: Record<string, [text: string, kind: string]> = {
  live: ["Live", "ok"], stale: ["Not current", "bad"], waiting: ["Waiting for data", "wait"],
  offline: ["Disconnected", "bad"], connecting: ["Connecting", "wait"],
};

export function TopBar() {
  const fresh = useFreshness();
  const summary = useTwin((s) => s.mirror?.summary);
  const origin = useTwin((s) => s.mirror?.meta.origin);
  const at = useTwin((s) => s.mirror?.meta.last_observation_at ?? null);
  const filter = useView((s) => s.statusFilter);
  const { left, right, bottom } = useView();
  const { setStatusFilter, set } = useView.getState();
  return (
    <header className="topbar">
      <div className="brand"><Icon name="layers" size={18} /><b>SimuCore</b><span className="muted">Banking digital twin</span></div>
      <span className={`state ${FRESH[fresh][1]}`} data-testid="freshness" data-state={fresh} title={at ? `Last measurement ${clock(at)}` : undefined}>
        <i />{FRESH[fresh][0]}
      </span>
      {origin === "synthetic" && <span className="state wait" title="Generated data, not measured">Synthetic data</span>}
      <nav className="counts" aria-label="Filter by status" data-testid="status-counts">
        {summary && STATUSES.map((s: Status) => {
          const n = summary.by_status[s] ?? 0;
          return (
            <button
              type="button" key={s} className={`count-btn ${filter === s ? "active" : ""}`} data-status={s} disabled={!n && filter !== s}
              aria-pressed={filter === s} title={`${STATUS[s].label}: ${n}. Click to highlight.`} onClick={() => setStatusFilter(s)}
            >
              <Icon name={STATUS[s].icon} size={13} color={STATUS[s].color} /><b>{n}</b><span>{STATUS[s].label}</span>
            </button>
          );
        })}
      </nav>
      <button type="button" className="search-btn" onClick={() => set({ searchOpen: true })} aria-label="Search components">
        <Icon name="search" size={14} /><span>Search components</span><kbd>Ctrl K</kbd>
      </button>
      <div className="toggles">
        <Button icon="panelLeft" label="Infrastructure panel" active={left} onClick={() => set({ left: !left })} />
        <Button icon="panelBottom" label="Alerts and events" active={bottom} onClick={() => set({ bottom: !bottom })} />
        <Button icon="panelRight" label="Inspector" active={right} onClick={() => set({ right: !right })} />
      </div>
    </header>
  );
}

// --------------------------------------------------------------------- banner
/** Says so when the scene does not show the present. */
export function Banner() {
  const fresh = useFreshness();
  const now = useNow();
  const at = useTwin((s) => s.mirror?.meta.last_observation_at ?? null);
  const [shown, setShown] = useState(false);
  useEffect(() => {
    // not for the blink of an eye while a connection is being made
    const t = setTimeout(() => setShown(fresh !== "live"), fresh === "live" ? 0 : 700);
    return () => clearTimeout(t);
  }, [fresh]);
  if (!shown || fresh === "live") return null;
  const last = at ? ` Last measurement ${clock(at)} (${ago(at, now)} ago).` : "";
  const texts: Record<string, string> = {
    offline: `No connection to the digital twin. The scene shows the last known state.${last} Reconnecting…`,
    stale: `The digital twin receives no measurements. Statuses are unknown until it does.${last}`,
    waiting: "The digital twin has not received any measurement yet.",
    connecting: "Connecting to the digital twin…",
  };
  return <div className="banner" role="alert" data-testid="banner" data-state={fresh}><Icon name="triangle" size={15} />{texts[fresh]}</div>;
}

// --------------------------------------------------------------------- search
export function Search() {
  const open = useView((s) => s.searchOpen);
  return open ? <SearchDialog /> : null;
}

function SearchDialog() {
  const components = useTwin((s) => s.mirror?.components);
  const ids = useTwin((s) => s.mirror?.componentIds);
  const [q, setQ] = useState("");
  const [i, setI] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const { set, select, hover } = useView.getState();
  const found = useMemo(() => search(q, (ids ?? []).map((id) => components![id])).slice(0, 40), [q, ids, components]);
  const at = Math.min(i, Math.max(0, found.length - 1));
  useEffect(() => input.current?.focus(), []);
  useEffect(() => {
    list.current?.children[at]?.scrollIntoView({ block: "nearest" });
  }, [at]);
  const close = () => {
    hover(null);
    set({ searchOpen: false });
  };
  const choose = (id: string) => {
    close();
    // what one searches for must become visible
    const v = useView.getState();
    const c = components?.[id];
    if (c && v.hiddenTypes.has(c.type)) v.toggleType(c.type);
    if (v.statusFilter && c && c.status !== v.statusFilter) v.setStatusFilter(null);
    select({ kind: "component", id }, { fly: true });
  };
  return (
    <div className="overlay" onPointerDown={(e) => { if (e.target === e.currentTarget) close(); }}>
      <div className="search" role="dialog" aria-label="Search components" data-testid="search">
        <div className="search-input">
          <Icon name="search" size={16} />
          <input
            ref={input} value={q} placeholder="Name, type, status, port…" role="combobox" aria-expanded="true" aria-controls="search-results" aria-activedescendant={found[at] ? `r-${found[at].id}` : undefined}
            onChange={(e) => { setQ(e.target.value); setI(0); }}
            onKeyDown={(e) => {
              if (e.key === "Escape") close();
              else if (e.key === "ArrowDown") setI(Math.min(at + 1, found.length - 1));
              else if (e.key === "ArrowUp") setI(Math.max(at - 1, 0));
              else if (e.key === "Enter" && found[at]) choose(found[at].id);
              else return;
              e.preventDefault();
              e.stopPropagation();
            }}
          />
          <kbd>Esc</kbd>
        </div>
        <ul ref={list} id="search-results" role="listbox">
          {found.map((c, n) => (
            <li key={c.id} id={`r-${c.id}`} role="option" aria-selected={n === at} data-id={c.id} className={n === at ? "active" : ""}
              onMouseEnter={() => { setI(n); hover(c.id); }} onClick={() => choose(c.id)}>
              <Icon name={iconOf(c)} size={16} />
              <span className="grow">{c.name}<small>{TYPE[c.type].label} · {c.id}</small></span>
              <StatusChip status={c.status} small />
            </li>
          ))}
          {found.length === 0 && <li className="empty">Nothing matches “{q}”.</li>}
        </ul>
      </div>
    </div>
  );
}

// -------------------------------------------------------------------- toolbar
const PRESETS: [Preset, string, string, string][] = [
  ["overview", "home", "Overview", "1"], ["front", "front", "Front view", "2"], ["side", "side", "Side view", "3"], ["top", "top", "View from above", "4"],
];

export function Toolbar() {
  const { structural, labels, quality } = useView();
  const { request, set } = useView.getState();
  return (
    <div className="toolbar" role="toolbar" aria-label="Camera and display" data-testid="toolbar">
      <div className="tool-group">
        {PRESETS.map(([p, icon, label, key]) => <Button key={p} icon={icon} label={label} kbd={key} onClick={() => request({ what: "preset", name: p })} />)}
      </div>
      <div className="tool-group">
        <Button icon="plus" label="Zoom in" kbd="+" onClick={() => request({ what: "step", name: "zoomIn" })} />
        <Button icon="minus" label="Zoom out" kbd="-" onClick={() => request({ what: "step", name: "zoomOut" })} />
        <Button icon="rotateLeft" label="Rotate left" kbd="←" onClick={() => request({ what: "step", name: "left" })} />
        <Button icon="rotateRight" label="Rotate right" kbd="→" onClick={() => request({ what: "step", name: "right" })} />
      </div>
      <div className="tool-group">
        <Button icon="cable" label="Cables to hosts, network and monitoring" kbd="C" active={structural} onClick={() => set({ structural: !structural })} />
        <Button icon="tag" label="Names" kbd="L" active={labels} onClick={() => set({ labels: !labels })} />
        <label className="quality" title="Detail of the drawing">
          <Icon name="gear" size={14} />
          <select aria-label="Detail" value={quality} onChange={(e) => set({ quality: e.target.value as typeof quality })}>
            <option value="auto">Auto</option><option value="high">High</option><option value="low">Low</option>
          </select>
        </label>
      </div>
    </div>
  );
}

/** What the current mode shows, in words. */
export function ModeNote() {
  const mode = useView((s) => s.mode);
  const id = useView((s) => (s.selected?.kind === "component" ? s.selected.id : null));
  const filter = useView((s) => s.statusFilter);
  const name = useTwin((s) => (id ? s.mirror?.components[id]?.name : undefined));
  const { setMode, setStatusFilter } = useView.getState();
  const what = mode === "isolate" && name ? <>Isolated: <b>{name}</b> and what it is directly connected to</>
    : mode === "dependencies" && name ? <><span className="muted">left: depends on it</span><b>{name}</b><span className="muted">right: it depends on</span></>
    : filter ? <>Highlighted: <b>{STATUS[filter].label}</b></> : null;
  if (!what) return null;
  return (
    <div className="mode-note" data-testid="mode-note">
      <span>{what}</span>
      <Button icon="close" label="Show everything" kbd="Esc" onClick={() => (mode !== "normal" ? setMode("normal") : setStatusFilter(null))} />
    </div>
  );
}

export function Legend() {
  const [open, setOpen] = useState(false);
  return (
    <div className={`legend ${open ? "open" : ""}`} data-testid="legend">
      <button type="button" className="fold" aria-expanded={open} onClick={() => setOpen(!open)}><Icon name="question" size={13} /> Legend</button>
      {open && (
        <dl>
          {STATUSES.map((s) => <div key={s}><dt><StatusChip status={s} small /></dt><dd>{LOOK[s]}</dd></div>)}
          <div><dt><Icon name="anomaly" size={13} color={AI_COLOR.anomaly} /> anomaly</dt><dd>ring of moving dashes: unusual now (Isolation Forest)</dd></div>
          <div><dt><Icon name="forecast" size={13} color={AI_COLOR.risk} /> prediction</dt><dd>arc, as long as the failure or degradation is probable within 2 minutes (XGBoost)</dd></div>
          <div><dt><Icon name="graph" size={13} color={IMPACT_COLOR} /> impact</dt><dd>outer band: solid = confirmed, hatched = predicted only (dependency graph); waves = where a failure starts; pulses along the connections show the propagation path</dd></div>
          <div><dt>Moving lights</dt><dd>requests; more and faster with more traffic (logarithmic), red = failing</dd></div>
          <div><dt>Blinking rows</dt><dd>activity of the servers, from the measured load</dd></div>
          <div><dt>Bar at the edge</dt><dd>measured load, 0 to 100 %</dd></div>
        </dl>
      )}
    </div>
  );
}

const LOOK: Record<Status, string> = {
  healthy: "steady light",
  warning: "slow pulse, triangle sign",
  overloaded: "fast pulse, glowing, waves, gauge sign",
  failed: "dark, flashing red, cross sign",
  recovering: "light running upwards",
  unknown: "grey, dashed ring",
};

// --------------------------------------------------------------------- drawer
export function Drawer() {
  const tab = useView((s) => s.bottomTab);
  const set = useView((s) => s.set);
  const n = useAlerts().length;
  const anomalies = useTwin((s) => s.mirror?.summary.anomalies.length ?? 0);
  const predicted = useTwin((s) => s.mirror?.summary.predicted.length ?? 0);
  const affected = useTwin((s) => s.mirror?.summary.propagation.affected ?? 0);
  const simulated = useImpact((s) => !!s.simulation);
  return (
    <section className={`panel bottom ${tab === "ai" || tab === "impact" ? "tall" : ""}`} aria-label="Alerts and events" data-testid="drawer">
      <Tabs value={tab} onChange={(t) => set({ bottomTab: t })} items={[
        ["alerts", <>Alerts <span className={`count ${n ? "hot" : ""}`} data-testid="alert-count">{n}</span></>],
        ["events", "Event history"],
        ["ai", <>AI insights <span className="count" data-testid="ai-count" title={`${anomalies} anomalies, ${predicted} predictive warnings`}>
          <Icon name="anomaly" size={11} color={AI_COLOR.anomaly} />{anomalies} <Icon name="forecast" size={11} color={AI_COLOR.risk} />{predicted}</span></>],
        ["impact", <>Failure impact <span className="count" data-testid="impact-count" title={simulated ? "a simulation is shown" : `${affected} components affected`}>
          <Icon name="graph" size={11} color={IMPACT_COLOR} />{simulated ? "sim" : affected}</span></>],
      ]} />
      <div className="scroll">{tab === "alerts" ? <Alerts /> : tab === "events" ? <EventList /> : tab === "ai" ? <AiPanel /> : <ImpactPanel />}</div>
    </section>
  );
}

// --------------------------------------------------------------------- toasts
export function Toasts() {
  const toasts = useTwin((s) => s.toasts);
  return <div className="toasts" aria-live="polite" data-testid="toasts">{toasts.map((t) => <ToastCard key={t.id} t={t} />)}</div>;
}

const TOAST = {
  predicted: { icon: "forecast", color: AI_COLOR.risk },
  anomaly: { icon: "anomaly", color: AI_COLOR.anomaly },
};

function ToastCard({ t }: { t: Toast }) {
  const dismiss = useTwin((s) => s.dismiss);
  const select = useView((s) => s.select);
  const set = useView((s) => s.set);
  const ai = t.status === "predicted" || t.status === "anomaly";
  const look = t.status === "predicted" || t.status === "anomaly" ? TOAST[t.status] : STATUS[t.status];
  useEffect(() => {
    const h = setTimeout(() => dismiss(t.id), t.status === "failed" || t.status === "overloaded" || t.status === "predicted" ? 9000 : 5000);
    return () => clearTimeout(h);
  }, [t.id, t.status, dismiss]);
  return (
    <div className="toast" data-status={t.status} data-object={t.objectId} style={{ "--c": look.color } as React.CSSProperties}>
      <Icon name={look.icon} size={18} color={look.color} />
      <button type="button" className="toast-text" onClick={() => { select({ kind: "component", id: t.objectId }, { fly: true }); if (ai) set({ rightTab: "ai" }); dismiss(t.id); }}>
        <b>{t.text}</b>{t.detail && <span>{t.detail}</span>}
      </button>
      <Button icon="close" label="Dismiss" onClick={() => dismiss(t.id)} />
    </div>
  );
}
