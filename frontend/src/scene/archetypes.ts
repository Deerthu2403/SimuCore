import { BoxGeometry, BufferGeometry, Color, CylinderGeometry, Float32BufferAttribute, Matrix4, PlaneGeometry, TorusGeometry } from "three";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";
import type { ComponentType } from "../twin/types";
import { SIZE } from "./layout";

/** One merged geometry per type of component, drawn as instances: hundreds of components of a type
 *  cost one draw call. ``aPart`` tells the shader what a surface is. */
export const PART = { body: 0, status: 1, face: 2, load: 3, trim: 4, screen: 5, field: 6, fan: 7 } as const;
type Part = (typeof PART)[keyof typeof PART];

const DARK = "#1c232e";
const MID = "#2b3543";
const LIGHT = "#566476";
const BLACK = "#0b0f15";

function tag(g: BufferGeometry, color: string, part: Part, m?: Matrix4): BufferGeometry {
  if (m) g.applyMatrix4(m);
  const n = g.getAttribute("position").count;
  const c = new Color(color);
  g.setAttribute("color", new Float32BufferAttribute(Array.from({ length: n * 3 }, (_, i) => [c.r, c.g, c.b][i % 3]), 3));
  g.setAttribute("aPart", new Float32BufferAttribute(new Array(n).fill(part), 1));
  return g;
}

const box = (w: number, h: number, d: number, x: number, y: number, z: number, color: string, part: Part = PART.body) =>
  tag(new BoxGeometry(w, h, d), color, part, new Matrix4().makeTranslation(x, y, z));

const panel = (w: number, h: number, x: number, y: number, z: number, part: Part, color = BLACK, turn = 0) =>
  tag(new PlaneGeometry(w, h), color, part, new Matrix4().makeTranslation(x, y, z).multiply(new Matrix4().makeRotationY(turn)));

const tube = (r0: number, r1: number, h: number, x: number, y: number, z: number, color: string, part: Part = PART.body, seg = 32) =>
  tag(new CylinderGeometry(r0, r1, h, seg, 1), color, part, new Matrix4().makeTranslation(x, y, z));

const ring = (r: number, t: number, x: number, y: number, z: number, color: string, part: Part, upright = false) =>
  tag(new TorusGeometry(r, t, 6, 36), color, part,
    new Matrix4().makeTranslation(x, y, z).multiply(new Matrix4().makeRotationX(upright ? 0 : Math.PI / 2)));

/** A 42U cabinet holding the application servers of one microservice. */
function rack(): BufferGeometry[] {
  const { w, h, d } = SIZE.microservice;
  const g = [
    box(w, 0.1, d, 0, 0.05, 0, BLACK),
    box(0.07, h - 0.2, d, -w / 2 + 0.035, h / 2, 0, DARK),
    box(0.07, h - 0.2, d, w / 2 - 0.035, h / 2, 0, DARK),
    box(w, 0.1, d, 0, h - 0.05, 0, MID),
    box(w - 0.14, h - 0.2, d - 0.16, 0, h / 2, -0.04, BLACK),
    // front door frame
    box(0.1, h - 0.2, 0.06, -w / 2 + 0.05, h / 2, d / 2 - 0.02, MID),
    box(0.1, h - 0.2, 0.06, w / 2 - 0.05, h / 2, d / 2 - 0.02, MID),
    box(w - 0.2, 0.08, 0.06, 0, h - 0.14, d / 2 - 0.02, MID),
    box(w - 0.2, 0.12, 0.06, 0, 0.16, d / 2 - 0.02, MID),
    // server units and light strips
    panel(w - 0.34, h - 0.42, 0.04, h / 2 + 0.01, d / 2 - 0.06, PART.face),
    box(0.055, h - 0.36, 0.04, -w / 2 + 0.05, h / 2 + 0.01, d / 2 + 0.02, BLACK, PART.status),
    box(0.055, h - 0.36, 0.04, w / 2 - 0.05, h / 2 + 0.01, d / 2 + 0.02, BLACK, PART.status),
    box(w - 0.1, 0.035, 0.035, 0, h - 0.012, d / 2 - 0.01, BLACK, PART.status),
    panel(0.1, h - 0.5, -w / 2 + 0.19, h / 2 + 0.01, d / 2 - 0.05, PART.load),
    // side panels with vents, back
    panel(d - 0.3, h - 0.5, w / 2 + 0.002, h / 2, 0, PART.fan, DARK, Math.PI / 2),
    panel(d - 0.3, h - 0.5, -w / 2 - 0.002, h / 2, 0, PART.fan, DARK, -Math.PI / 2),
    panel(w - 0.3, h - 0.4, 0, h / 2, -d / 2 + 0.035, PART.face, BLACK, Math.PI),
    // cable inlet on the roof
    box(0.5, 0.07, 0.4, 0, h + 0.02, -0.1, LIGHT),
  ];
  for (const x of [-1, 1]) for (const z of [-1, 1]) g.push(box(0.12, 0.05, 0.12, x * (w / 2 - 0.12), 0.0, z * (d / 2 - 0.12), LIGHT));
  return g;
}

/** Storage: stacked platters with a status ring between them. */
function storage(): BufferGeometry[] {
  const r = SIZE.database.w / 2;
  const g = [tube(r, r + 0.04, 0.14, 0, 0.07, 0, BLACK)];
  for (let i = 0; i < 3; i++) {
    const y = 0.48 + i * 0.66;
    g.push(tube(r - 0.12, r - 0.12, 0.5, 0, y, 0, MID, PART.body, 40));
    g.push(tube(r - 0.05, r - 0.05, 0.06, 0, y + 0.25, 0, LIGHT, PART.body, 40));
    g.push(tube(r - 0.07, r - 0.07, 0.06, 0, y - 0.28, 0, BLACK, PART.status, 40));
    for (let k = 0; k < 10; k++) {
      const a = (k / 10) * Math.PI * 2;
      g.push(panel(0.36, 0.34, Math.sin(a) * (r - 0.105), y, Math.cos(a) * (r - 0.105), PART.face, BLACK, a));
    }
  }
  g.push(tube(r - 0.3, r - 0.25, 0.1, 0, 2.22, 0, DARK));
  g.push(box(0.16, 1.5, 0.05, 0, 1.1, r - 0.03, BLACK));
  g.push(panel(0.1, 1.4, 0, 1.1, r - 0.002, PART.load));
  return g;
}

/** A blade enclosure: the machine services run on. */
function host(): BufferGeometry[] {
  const { w, h, d } = SIZE.server;
  const g = [
    box(w, 0.12, d, 0, 0.06, 0, BLACK),
    box(w, h - 0.12, d, 0, h / 2 + 0.06, -0.04, DARK),
    box(w + 0.04, 0.09, d + 0.04, 0, h - 0.02, 0, MID),
    box(w - 0.1, 0.04, 0.04, 0, h - 0.1, d / 2 - 0.02, BLACK, PART.status),
    box(w - 0.1, 0.04, 0.04, 0, 0.17, d / 2 - 0.02, BLACK, PART.status),
    panel(0.12, h - 0.7, -w / 2 + 0.14, h / 2 + 0.05, d / 2 - 0.035, PART.load),
  ];
  for (let i = 0; i < 8; i++) {
    const x = -w / 2 + 0.38 + i * 0.235;
    g.push(box(0.2, h - 0.72, 0.08, x, h / 2 + 0.16, d / 2 - 0.02, BLACK));
    g.push(panel(0.17, h - 0.8, x, h / 2 + 0.16, d / 2 + 0.022, PART.face));
  }
  for (let i = 0; i < 5; i++) g.push(panel(0.3, 0.2, -0.72 + i * 0.36, 0.34, d / 2 - 0.03, PART.fan));
  g.push(panel(d - 0.2, h - 0.5, w / 2 + 0.002, h / 2, -0.04, PART.fan, DARK, Math.PI / 2));
  g.push(panel(d - 0.2, h - 0.5, -w / 2 - 0.002, h / 2, -0.04, PART.fan, DARK, -Math.PI / 2));
  return g;
}

/** The network core: a low switch with rows of ports and an antenna-like ring. */
function core(): BufferGeometry[] {
  const { w, h, d } = SIZE.network;
  return [
    box(w, 0.2, d, 0, 0.1, 0, BLACK),
    box(w - 0.2, h - 0.35, d - 0.2, 0, h / 2 + 0.02, 0, DARK),
    box(w - 0.12, 0.08, d - 0.12, 0, h - 0.16, 0, MID),
    panel(w - 0.5, 0.26, 0, h / 2 + 0.2, d / 2 - 0.095, PART.face),
    panel(w - 0.5, 0.26, 0, h / 2 - 0.16, d / 2 - 0.095, PART.face),
    panel(w - 0.5, 0.26, 0, h / 2 + 0.2, -d / 2 + 0.095, PART.face, BLACK, Math.PI),
    box(w - 0.3, 0.035, 0.03, 0, h / 2 + 0.02, d / 2 - 0.09, BLACK, PART.status),
    box(w - 0.04, 0.04, 0.04, 0, 0.21, d / 2 - 0.02, BLACK, PART.status),
    tube(0.1, 0.16, 0.12, 0, h - 0.08, 0, LIGHT),
    ring(0.42, 0.035, 0, h + 0.02, 0, BLACK, PART.status),
    ring(0.24, 0.025, 0, h + 0.06, 0, BLACK, PART.trim),
  ];
}

/** Client entry: a column from which traffic fans out. */
function balancer(): BufferGeometry[] {
  const { w, h } = SIZE.load_balancer;
  const r = w / 2;
  const g = [
    tube(r, r + 0.05, 0.16, 0, 0.08, 0, BLACK, PART.body, 6),
    tube(r - 0.28, r - 0.1, 0.4, 0, 0.36, 0, DARK, PART.body, 6),
    tube(0.34, 0.5, h - 1.1, 0, h / 2 + 0.05, 0, DARK, PART.body, 6),
    tube(0.62, 0.36, 0.36, 0, h - 0.4, 0, MID, PART.body, 6),
    tube(0.6, 0.6, 0.07, 0, h - 0.18, 0, MID, PART.body, 6),
    ring(0.62, 0.035, 0, h - 0.18, 0, BLACK, PART.status),
    ring(r - 0.2, 0.035, 0, 0.58, 0, BLACK, PART.status),
    tube(0.3, 0.5, 0.16, 0, h - 0.07, 0, MID, PART.body, 6),
  ];
  for (let i = 0; i < 6; i++) {
    const a = ((i + 0.5) / 6) * Math.PI * 2;
    const m = new Matrix4().makeRotationY(a);
    for (const part of [
      box(0.1, 1.2, 0.42, 0, 1.28, 0.58, MID),
      panel(0.06, 1.1, 0, 1.28, 0.795, PART.trim),
      panel(0.3, 0.9, 0, 1.45, 0.462, PART.face),
    ]) g.push(part.applyMatrix4(m));
  }
  return g;
}

/** An external service: a portal at the bank's boundary. */
function portal(): BufferGeometry[] {
  const { w, h, d } = SIZE.gateway;
  return [
    box(w, 0.14, d, 0, 0.07, 0, BLACK),
    box(0.42, h - 0.5, d - 0.3, -w / 2 + 0.26, h / 2 - 0.1, 0, DARK),
    box(0.42, h - 0.5, d - 0.3, w / 2 - 0.26, h / 2 - 0.1, 0, DARK),
    box(w - 0.1, 0.4, d - 0.3, 0, h - 0.24, 0, MID),
    box(0.05, h - 0.75, 0.04, -w / 2 + 0.26, h / 2 - 0.1, d / 2 - 0.14, BLACK, PART.status),
    box(0.05, h - 0.75, 0.04, w / 2 - 0.26, h / 2 - 0.1, d / 2 - 0.14, BLACK, PART.status),
    box(w - 0.5, 0.045, 0.04, 0, h - 0.24, d / 2 - 0.14, BLACK, PART.status),
    panel(w - 0.94, h - 0.6, 0, h / 2 - 0.15, 0, PART.field),
    panel(w - 0.94, h - 0.6, 0, h / 2 - 0.15, -0.01, PART.field, BLACK, Math.PI),
    ring(0.62, 0.03, 0, h / 2 - 0.15, 0.02, BLACK, PART.trim, true),
  ];
}

/** Monitoring: an operator's desk with screens. */
function consoleDesk(): BufferGeometry[] {
  const { w, d } = SIZE.monitoring;
  const g = [
    box(w, 0.07, d * 0.62, 0, 0.78, 0.1, MID),
    box(0.08, 0.76, d * 0.56, -w / 2 + 0.1, 0.38, 0.1, DARK),
    box(0.08, 0.76, d * 0.56, w / 2 - 0.1, 0.38, 0.1, DARK),
    box(w - 0.2, 0.5, 0.04, 0, 0.5, -0.24, DARK),
    box(w - 0.1, 0.03, 0.03, 0, 0.745, d * 0.31 + 0.11, BLACK, PART.status),
  ];
  for (const [x, turn] of [[-0.76, 0.32], [0, 0], [0.76, -0.32]] as const) {
    const m = new Matrix4().makeTranslation(x, 1.32, -0.16 + Math.abs(x) * 0.16).multiply(new Matrix4().makeRotationY(turn));
    g.push(tag(new BoxGeometry(0.74, 0.5, 0.05), BLACK, PART.body, m));
    g.push(tag(new PlaneGeometry(0.69, 0.45), BLACK, PART.screen, m.clone().multiply(new Matrix4().makeTranslation(0, 0, 0.028))));
    g.push(box(0.06, 0.3, 0.06, x, 0.95, -0.2 + Math.abs(x) * 0.16, LIGHT));
  }
  g.push(box(0.5, 0.03, 0.2, 0, 0.83, 0.25, BLACK));
  return g;
}

const BUILD: Record<ComponentType, () => BufferGeometry[]> = {
  microservice: rack, database: storage, server: host, network: core,
  load_balancer: balancer, gateway: portal, monitoring: consoleDesk,
};

const cache = new Map<ComponentType, BufferGeometry>();

export function archetype(type: ComponentType): BufferGeometry {
  let g = cache.get(type);
  if (!g) {
    const parts = BUILD[type]();
    const merged = mergeGeometries(parts);
    if (!merged) throw new Error(`geometry of ${type} cannot be merged`);
    parts.forEach((p) => p.dispose());
    merged.computeBoundingSphere();
    cache.set(type, (g = merged));
  }
  return g;
}
