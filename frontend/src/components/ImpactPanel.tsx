import { useTwinStore } from "../store/twinStore";

export function ImpactPanel() {
  const impact = useTwinStore((s) => s.impact);
  const { setImpact, select } = useTwinStore.getState();
  if (!impact) return null;
  const downstream = impact.result.impacted.filter((n) => n.depth > 0);

  return (
    <section className="panel impact">
      <div className="impact-head">
        <h3>Blast radius — {impact.result.origin.join(", ")}</h3>
        <button className="ghost" onClick={() => setImpact(null)}>✕</button>
      </div>
      <p className="explanation">{impact.explanation.summary}</p>
      <p className="muted">
        {downstream.length} impacted · business impact {impact.result.business_impact.toFixed(2)}
      </p>
      <ol className="impact-list">
        {downstream.slice(0, 12).map((n) => (
          <li key={n.node_id} onClick={() => select(n.node_id)}>
            <span className="bar" style={{ width: `${n.impact * 100}%` }} />
            <span className="name">{n.node_id}</span>
            <span className="val">{(n.impact * 100).toFixed(0)}%</span>
          </li>
        ))}
      </ol>
    </section>
  );
}
