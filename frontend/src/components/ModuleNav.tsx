import { useMemo } from "react";
import { LAYER_LABELS, presentLayers } from "../scene/layout";
import { useTwinStore } from "../store/twinStore";

const MODULES: { name: string; phase: number }[] = [
  { name: "Monitor", phase: 1 },
  { name: "Impact analysis", phase: 1 },
  { name: "Predict", phase: 3 },
  { name: "What-if", phase: 4 },
  { name: "Optimize", phase: 5 },
  { name: "Calibrate", phase: 5 },
  { name: "Recommend", phase: 5 },
];

export function ModuleNav() {
  const hidden = useTwinStore((s) => s.hiddenLayers);
  const toggle = useTwinStore((s) => s.toggleLayer);
  const topology = useTwinStore((s) => s.topology);
  const layers = useMemo(() => presentLayers(topology?.nodes ?? []), [topology]);
  return (
    <nav className="panel module-nav">
      <h3>Modules</h3>
      <ul>
        {MODULES.map((m) => (
          <li key={m.name} className={m.phase > 1 ? "disabled" : "active"} title={m.phase > 1 ? `Planned for phase ${m.phase}` : ""}>
            {m.name}
            {m.phase > 1 && <span className="phase-tag">P{m.phase}</span>}
          </li>
        ))}
      </ul>
      <h3>Layers</h3>
      {layers.map((l) => (
        <label key={l} className="layer-toggle">
          <input type="checkbox" checked={!hidden.has(l)} onChange={() => toggle(l)} />
          {LAYER_LABELS[l]}
        </label>
      ))}
    </nav>
  );
}
