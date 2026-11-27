import { useEffect, useMemo } from "react";
import {
  AI_COLOR, CONDITION, isAnomalous, loadAi, metricName, percent, TARGET, topRisk, topWarning, useAi,
  type StoredAnomaly, type StoredWarning,
} from "../twin/ai";
import { ago, clock } from "../twin/format";
import { useTwin } from "../twin/store";
import type { Insight, TwinComponent } from "../twin/types";
import { useView } from "../twin/view";
import { useNow } from "./hooks";
import { Icon } from "./Icon";
import { Sparkline } from "./Sparkline";

const UNITS = { ai_anomaly: "ratio", ai_risk_degradation: "ratio", ai_risk_failure: "ratio" };

/** Loads history when it is looked at, and again when something happens. */
function useHistory(): void {
  const origin = useTwin((s) => s.mirror?.meta.origin);
  const ai = useTwin((s) => s.mirror?.summary.ai);
  const marks = useTwin((s) => {
    const m = s.mirror;
    return m ? `${m.summary.anomalies.join()}|${m.summary.predicted.join()}` : "";
  });
  useEffect(() => {
    if (!origin) return;
    void loadAi(origin);
    // the service writes within a second; and outcomes become known later
    const soon = setTimeout(() => void loadAi(origin), 2500);
    const often = setInterval(() => void loadAi(origin), 15_000);
    return () => {
      clearTimeout(soon);
      clearInterval(often);
    };
  }, [origin, marks, ai]);
}

const useServices = () => {
  const list = useTwin((s) => s.mirror?.components);
  return useMemo(() => Object.values(list ?? {}).filter((c) => c.type === "microservice"), [list]);
};

function open(id: string) {
  const v = useView.getState();
  v.select({ kind: "component", id }, { fly: true });
  v.set({ rightTab: "ai" });
}

export function Tag({ kind, children }: { kind: "anomaly" | "risk"; children: React.ReactNode }) {
  return (
    <span className="ai-tag" style={{ "--c": AI_COLOR[kind] } as React.CSSProperties}>
      <Icon name={kind === "anomaly" ? "anomaly" : "forecast"} size={12} color={AI_COLOR[kind]} />{children}
    </span>
  );
}

function Bar({ value, limit, kind, label }: { value: number; limit?: number; kind: "anomaly" | "risk"; label: string }) {
  return (
    <span className="ai-bar" role="meter" aria-label={label} aria-valuenow={Math.round(value * 100)} aria-valuemin={0} aria-valuemax={100}>
      <i style={{ width: `${value * 100}%`, background: AI_COLOR[kind] }} />
      {limit !== undefined && <u style={{ left: `${limit * 100}%` }} title={`threshold ${percent(limit)}`} />}
    </span>
  );
}

// ------------------------------------------------------------------ the panel
export function AiPanel() {
  useHistory();
  const ai = useTwin((s) => s.mirror?.summary.ai);
  const state = useAi((s) => s.state);
  if (ai === "absent") {
    return (
      <div className="ai-panel" data-testid="ai-panel" data-ai="absent">
        <p className="note warn">The AI service does not report. There are no anomalies or predictions to show; statuses and measurements are not affected.</p>
        <History />
      </div>
    );
  }
  return (
    <div className="ai-panel" data-testid="ai-panel" data-ai={ai}>
      <Detected />
      <Predicted />
      <Contributing />
      <History />
      {state === "unavailable" && <p className="note warn">The history of the AI service is unavailable.</p>}
      <Quality />
    </div>
  );
}

function Detected() {
  const services = useServices();
  const now = useNow();
  const found = services.filter((c) => isAnomalous(c)).sort((a, b) => b.insight!.anomaly!.score - a.insight!.anomaly!.score);
  return (
    <section aria-label="Detected anomalies" data-testid="ai-anomalies">
      <h3><Icon name="anomaly" size={14} color={AI_COLOR.anomaly} /> Detected anomalies <span className="count">{found.length}</span></h3>
      <p className="sub-note">Isolation Forest, unsupervised: unusual <b>now</b>, not a forecast</p>
      {found.length === 0 && <p className="empty">Nothing unusual.</p>}
      <ul>
        {found.map((c) => {
          const a = c.insight!.anomaly!;
          return (
            <li key={c.id}>
              <button type="button" className="ai-row" data-object={c.id} onClick={() => open(c.id)}>
                <span className="grow">{c.name.replace(/ Service$/, "")}</span>
                <span className="muted">{CONDITION[a.condition] ?? a.condition}</span>
                <Bar value={a.score} limit={0.5} kind="anomaly" label="anomaly score" />
                <span className="num">{a.score.toFixed(2)}</span>
                <time>{ago(a.since, now)}</time>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function Predicted() {
  const services = useServices();
  const now = useNow();
  const model = useAi((s) => s.model);
  const list = [...services].sort((a, b) => (topWarning(b) ? 1 : 0) - (topWarning(a) ? 1 : 0)
    || (topRisk(b)?.probability ?? -1) - (topRisk(a)?.probability ?? -1));
  const n = services.filter((c) => topWarning(c)).length;
  return (
    <section aria-label="Predicted failures" data-testid="ai-predictions">
      <h3><Icon name="forecast" size={14} color={AI_COLOR.risk} /> Predicted degradation and failure <span className={`count ${n ? "hot" : ""}`}>{n}</span></h3>
      <p className="sub-note">XGBoost, supervised: probability within the next {model ? model.horizon_s / 60 : 2} minutes</p>
      <table className="numbers ai-table">
        <thead><tr><th>Service</th><th>Degradation</th><th>Failure</th><th>Warning</th></tr></thead>
        <tbody>
          {list.map((c) => {
            const w = topWarning(c);
            return (
              <tr key={c.id} data-id={c.id} data-warning={w?.target ?? ""} tabIndex={0} onClick={() => open(c.id)} onKeyDown={(e) => { if (e.key === "Enter") open(c.id); }}>
                <th scope="row">{c.name.replace(/ Service$/, "")}</th>
                {(["degradation", "failure"] as const).map((t) => <RiskCell key={t} c={c} target={t} limit={model?.thresholds[t]} />)}
                <td>{w ? <Tag kind="risk">since {ago(w.opened_at, now)}</Tag> : <span className="muted">–</span>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

function RiskCell({ c, target, limit }: { c: TwinComponent; target: "degradation" | "failure"; limit?: number }) {
  const r = c.insight?.risks.find((x) => x.target === target);
  if (!r) return <td className="muted" title={c.insight?.risk_reason}>–</td>;
  if (r.probability === null) return <td className="muted">{r.level === "present" ? "present" : "just over"}</td>;
  return (
    <td data-target={target} data-value={r.probability}>
      <Bar value={r.probability} limit={limit} kind="risk" label={`${TARGET[target]} probability`} /> {percent(r.probability)}
    </td>
  );
}

/** Which measurements matter: for the selected service, or the one most at risk. */
function Contributing() {
  const services = useServices();
  const selected = useView((s) => (s.selected?.kind === "component" ? s.selected.id : null));
  const c = services.find((x) => x.id === selected)
    ?? services.find((x) => topWarning(x)) ?? services.find((x) => isAnomalous(x));
  return (
    <section aria-label="Important contributing metrics" data-testid="ai-reasons">
      <h3>Important metrics {c && <span className="muted">{c.name.replace(/ Service$/, "")}</span>}</h3>
      {!c && <p className="empty">Select a service, or wait until something is unusual.</p>}
      {c && <Reasons insight={c.insight} />}
    </section>
  );
}

function Reasons({ insight }: { insight: Insight | null }) {
  if (!insight) return <p className="empty">No opinion.</p>;
  const why = insight.anomaly?.contributions ?? [];
  const top = Math.max(0.01, ...insight.reasons.map((r) => Math.abs(r.contribution)));
  return (
    <>
      {why.length > 0 && (
        <>
          <h4>Why unusual now</h4>
          <ul className="reasons">
            {why.map((w) => (
              <li key={w.metric}><span className="grow">{metricName(w.metric)}</span>
                <span className="muted">{w.deviation > 0 ? "+" : ""}{w.deviation.toFixed(1)} × normal spread</span>
                <Bar value={w.share} kind="anomaly" label="share of the anomaly score" /><span className="num">{percent(w.share)}</span></li>
            ))}
          </ul>
        </>
      )}
      {insight.reasons.length > 0 && (
        <>
          <h4>Why this risk</h4>
          <ul className="reasons">
            {insight.reasons.map((r) => (
              <li key={r.text}><span className="grow">{r.text}</span>
                <span className="muted">{r.contribution > 0 ? "raises" : "lowers"}</span>
                <Bar value={Math.abs(r.contribution) / top} kind="risk" label="influence" /><span className="num">{r.contribution > 0 ? "+" : "−"}{Math.abs(r.contribution).toFixed(2)}</span></li>
            ))}
          </ul>
        </>
      )}
      {why.length === 0 && insight.reasons.length === 0 && <p className="empty">Nothing unusual and no raised risk.</p>}
    </>
  );
}

const OUTCOME: Record<StoredWarning["outcome"], string> = {
  open: "open", came_true: "came true", did_not_happen: "did not happen", unresolved: "not known",
};

function History({ node }: { node?: string }) {
  const warnings = useAi((s) => s.warnings);
  const anomalies = useAi((s) => s.anomalies);
  const names = useTwin((s) => s.mirror?.components);
  const rows = useMemo(() => {
    const w = warnings.filter((x) => !node || x.node_id === node).map((x) => ({ t: x.opened_at, w: x as StoredWarning | null, a: null as StoredAnomaly | null }));
    const a = anomalies.filter((x) => !node || x.node_id === node).map((x) => ({ t: x.opened_at, w: null as StoredWarning | null, a: x as StoredAnomaly | null }));
    return [...w, ...a].sort((p, q) => Date.parse(q.t) - Date.parse(p.t)).slice(0, 80);
  }, [warnings, anomalies, node]);
  return (
    <section aria-label="Prediction history" data-testid={node ? "ai-node-history" : "ai-history"}>
      <h3>History <span className="muted">of warnings and anomalies</span></h3>
      {rows.length === 0 && <p className="empty">Nothing yet.</p>}
      <ol className="ai-history">
        {rows.map(({ w, a }) => {
          const id = (w ?? a)!.node_id;
          return (
            <li key={(w ?? a)!.id} title={w ? "" : (CONDITION[a!.condition] ?? "")}>
              <button type="button" className="ai-row" data-object={id} data-kind={w ? "warning" : "anomaly"} data-outcome={w?.outcome ?? ""} onClick={() => open(id)}>
                <time>{clock((w ?? a)!.opened_at)}</time>
                {w ? <Tag kind="risk">{TARGET[w.target]}</Tag> : <Tag kind="anomaly">anomaly</Tag>}
                {!node && <span className="grow">{(names?.[id]?.name ?? id).replace(/ Service$/, "")}</span>}
                <span className={node ? "grow muted" : "muted"}>
                  {w ? `${percent(w.probability)} → ${OUTCOME[w.outcome]}${w.lead_s ? ` after ${Math.round(w.lead_s)} s` : ""}` : `${CONDITION[a!.condition] ?? a!.condition}, score ${a!.score.toFixed(2)}`}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

const pc = (x: number | null | undefined) => (x === null || x === undefined ? "–" : `${Math.round(x * 100)} %`);

/** What is known about the quality: measured, with its source. */
function Quality() {
  const m = useAi((s) => s.model);
  const p = useAi((s) => s.performance);
  if (!m) return null;
  const v = m.validation;
  return (
    <section aria-label="Model quality" data-testid="ai-quality" className="wide">
      <h3>How good are the models? <span className="muted">version {m.version}, trained on {m.trained_on}</span></h3>
      <table className="numbers">
        <thead><tr><th>Tested on</th><th>Model</th><th>Precision</th><th>Recall</th><th>F1</th><th>ROC-AUC</th><th>Failures announced</th><th>False alarms</th><th>Median lead</th></tr></thead>
        <tbody>
          <Row on="synthetic test set" model="anomaly" j={m.test.anomaly} />
          {(["degradation", "failure"] as const).map((t) => <Row key={t} on="synthetic test set" model={t} j={m.test[t].xgboost} e={m.test[t].events} />)}
          {v && <Row on={`real simulated bank, ${v.recordings} runs`} model="anomaly" j={v.anomaly} />}
          {v && (["degradation", "failure"] as const).map((t) => <Row key={t} on={`real simulated bank, ${v.recordings} runs`} model={t} j={v[t].rows} e={v[t].events} />)}
        </tbody>
      </table>
      <p className="sub-note">
        {p && `Last 24 h in operation: ${(["degradation", "failure"] as const).map((t) => {
          const h = p.history[t];
          return `${t}: ${h.warnings.came_true ?? 0} warnings came true, ${h.warnings.did_not_happen ?? 0} did not, ${h.predictions} predictions checked`;
        }).join("; ")}. `}
        Probabilities are calibrated on synthetic data. Sudden crashes without any sign beforehand cannot be predicted.
      </p>
    </section>
  );
}

function Row({ on, model, j, e }: {
  on: string; model: string; j: { precision: number | null; recall: number | null; f1: number | null; roc_auc: number | null };
  e?: { failures: number; announced: number; alarms: number; false_alarms: number; lead_s_median: number | null };
}) {
  return (
    <tr><th scope="row">{on}</th><td>{model}</td><td>{pc(j.precision)}</td><td>{pc(j.recall)}</td><td>{j.f1?.toFixed(2) ?? "–"}</td><td>{j.roc_auc?.toFixed(2) ?? "–"}</td>
      <td>{e ? `${e.announced} of ${e.failures}` : "–"}</td><td>{e ? `${e.false_alarms} of ${e.alarms}` : "–"}</td><td>{e?.lead_s_median ? `${Math.round(e.lead_s_median)} s` : "–"}</td></tr>
  );
}

// -------------------------------------------------------------- one component
export function AiTab({ c }: { c: TwinComponent }) {
  useHistory();
  const model = useAi((s) => s.model);
  const ai = useTwin((s) => s.mirror?.summary.ai);
  const i = c.insight;
  if (c.type !== "microservice") return <p className="empty">The models look at the eight banking services only.</p>;
  if (!i) return <p className="note warn">{ai === "absent" ? "The AI service does not report." : "No opinion about this service."}</p>;
  const w = topWarning(c);
  return (
    <div data-testid="ai-tab">
      {w && (
        <p className="ai-alert risk"><Icon name="forecast" size={16} color={AI_COLOR.risk} />
          <span><b>{TARGET[w.target] === "failure" ? "Failure" : "Degradation"} predicted</b> within {w.horizon_s / 60} min, {percent(i.risks.find((r) => r.target === w.target)?.probability ?? w.peak)}. Warned at {clock(w.opened_at)}.</span></p>
      )}
      {i.anomaly?.since && (
        <p className="ai-alert anomaly"><Icon name="anomaly" size={16} color={AI_COLOR.anomaly} />
          <span><b>Anomaly</b> since {clock(i.anomaly.since)}: {CONDITION[i.anomaly.condition] ?? i.anomaly.condition}</span></p>
      )}
      <div className="tiles">
        <Score title="Anomaly score" id={c.id} metric="ai_anomaly" value={i.anomaly?.score ?? null} limit={0.5} kind="anomaly" none={i.anomaly_reason} digits />
        {(["degradation", "failure"] as const).map((t) => {
          const r = i.risks.find((x) => x.target === t);
          return <Score key={t} title={`Risk of ${t}`} id={c.id} metric={`ai_risk_${t}`} value={r?.probability ?? null} limit={model?.thresholds[t]} kind="risk" none={r ? (r.level === "present" ? "present now" : "just over") : i.risk_reason} />;
        })}
      </div>
      <Reasons insight={i} />
      <History node={c.id} />
      <p className="note">The anomaly score says how unusual this moment is (0.5 = threshold). The risk is the estimated probability within the next {i.risks[0]?.horizon_s ?? 120} s. Neither changes the status, which rests on measurements.</p>
    </div>
  );
}

function Score({ title, id, metric, value, limit, kind, none, digits }: {
  title: string; id: string; metric: string; value: number | null; limit?: number; kind: "anomaly" | "risk"; none: string; digits?: boolean;
}) {
  return (
    <div className="tile" data-metric={metric} data-value={value ?? ""}>
      <span className="tile-label">{title}</span>
      {value === null ? <span className="tile-value none">{none || "not known"}</span> : (
        <>
          <span className="tile-value">{digits ? value.toFixed(2) : percent(value)}</span>
          <Bar value={value} limit={limit} kind={kind} label={title} />
        </>
      )}
      <Sparkline id={id} metric={metric} units={UNITS} label={title} fixedTop={1} />
    </div>
  );
}
