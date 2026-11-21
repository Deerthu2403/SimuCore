import { STATUS_COLORS } from "../store/twinStore";

const SHAPES = [
  ["◆", "gateway"], ["■", "service"], ["⬤", "database (cylinder)"], ["◯", "cache"],
  ["▬", "queue"], ["▮", "server"], ["⬢", "network device"],
];

export function Legend() {
  return (
    <footer className="panel legend">
      {Object.entries(STATUS_COLORS).map(([s, c]) => (
        <span key={s}><i style={{ background: c }} /> {s}</span>
      ))}
      <span className="sep" />
      {SHAPES.map(([g, l]) => <span key={l}>{g} {l}</span>)}
      <span className="sep" />
      <span><i style={{ background: "#d946ef" }} /> blast radius</span>
    </footer>
  );
}
