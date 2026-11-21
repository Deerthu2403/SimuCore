# 06 — 3D User Interface Design

Theme: dark Network-Operations-Centre (NOC) aesthetic, high-contrast status
colours, readable at a distance on a wall display.

## 1. Screen layout

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ TopBar: SimuCore ● Live | twin v1234 | 27 nodes | 2 degraded | 0 down | mode  │
├──────────┬───────────────────────────────────────────────────────┬───────────┤
│ Module   │                                                       │ Inspector │
│ nav      │              3D DIGITAL TWIN SCENE                    │ (selected │
│          │                                                       │  node)    │
│ Monitor  │   layer planes stacked on Y:                          │ metrics   │
│ Predict  │     edge (gateways)                                   │ deps      │
│ Impact   │     application (microservices)                       │ dependents│
│ What-if  │     data (DBs, cache, queue)                          │ actions   │
│ Optimize │     compute (servers)                                 ├───────────┤
│ Calibrate│     network (LB, firewall, switches)                  │ Impact /  │
│ Recommend│                                                       │ Recommend │
├──────────┴───────────────────────────────────────────────────────┴───────────┤
│ Legend (status colours, node shapes)          Timeline scrubber (Phase 3)     │
└──────────────────────────────────────────────────────────────────────────────┘
```

## 2. Visual language

| Element | Encoding |
|---|---|
| Layer | horizontal translucent plane at its own height; label on the plane edge |
| Node kind | geometry: gateway = octahedron, service = rounded box, database = cylinder, cache = torus-ish capsule, queue = elongated box, server = tall box, network = icosahedron/flat disc |
| Health status | emissive colour — healthy `#22c55e`, degraded `#f59e0b`, critical `#ef4444`, down `#7f1d1d` pulsing, unknown `#64748b` translucent |
| Load (cpu / rps) | node scale 1.0 → 1.35 |
| Criticality | glow ring radius (1–5) |
| Dependency edge | line source → target; colour by edge kind; opacity by weight; animated flow particles along `calls` edges (Phase 3) |
| Blast radius | impacted nodes outlined in magenta, intensity = impact score; impact path edges highlighted |
| Anomaly | pulsing halo + icon billboard (Phase 3) |
| Prediction | floating probability badge above at-risk node (Phase 3) |
| Simulation mode | scene background shifts to deep blue + "SIMULATION" watermark so users never confuse the what-if copy with live state |

## 3. Interactions

| Interaction | Result |
|---|---|
| Orbit / pan / zoom | `OrbitControls` (drei) with damping |
| Hover node | tooltip: name, status, key metrics |
| Click node | select → Inspector panel, highlight 1-hop dependencies (cyan) and dependents (violet) |
| "Simulate failure" in Inspector | POST `/propagation/analyze` → blast-radius overlay |
| Double-click node | camera fly-to (Phase 3) |
| Layer toggles | show/hide layers to reduce clutter |
| Timeline scrubber | replay `node_state_snapshot` history (Phase 3) |
| What-if panel | build scenario (pick nodes, fault type, load multiplier) → run → watch simulated timeline (Phase 4) |
| Optimisation panel | Pareto front scatter (cost / latency / risk); selecting a point previews the configuration as ghost nodes in 3D (Phase 5) |
| Recommendation card | click "Why?" → Explanation drawer with factor bar chart + highlighted evidence path in 3D (Phase 5) |

## 4. Accessibility

* Status never encoded by colour alone — the Inspector and tooltips show text
  status; down nodes pulse; legend includes shapes.
* All panels keyboard-navigable; 3D selection mirrored in a searchable node list.
* Respect `prefers-reduced-motion` (disable pulses/particles).

## 5. Grafana's role

Grafana is used for **time-series analytics** (long-range metric charts,
ML model performance, calibration error trends) — not for the twin itself.
The 3D UI deep-links to Grafana dashboards per node (`/d/simucore-node?var-node=<id>`).
