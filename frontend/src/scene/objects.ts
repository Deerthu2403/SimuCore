import {
  BufferGeometry, CanvasTexture, Color, DataTexture, DynamicDrawUsage, Float32BufferAttribute, FloatType, Group,
  InstancedBufferAttribute, InstancedBufferGeometry, InstancedMesh, LinearFilter, LinearMipmapLinearFilter,
  Matrix4, Mesh, NearestFilter, PlaneGeometry, RGBAFormat, type IUniform, type Material,
} from "three";
import { ICONS, ICON_NAMES, iconOf, type IconName } from "../twin/icons";
import { COMPONENT_TYPES, type ComponentType, type Status } from "../twin/types";
import { AI_COLOR } from "../twin/ai";
import { STATUS } from "../twin/format";
import { archetype } from "./archetypes";
import { SIZE } from "./layout";
import {
  componentMaterial, LINK_COLS, PER_LINK, particleMaterial, plateMaterial, ringMaterial, wireMaterial, zoneMaterial,
} from "./shaders";
import { LINK_CODE, STATUS_CODE, type Node, type World } from "./world";

const NO_RAYCAST = () => undefined;

function attribute(count: number, size: number): InstancedBufferAttribute {
  const a = new InstancedBufferAttribute(new Float32Array(Math.max(1, count) * size), size);
  a.setUsage(DynamicDrawUsage);
  return a;
}

function quads(count: number, attrs: Record<string, InstancedBufferAttribute>): InstancedBufferGeometry {
  const g = new InstancedBufferGeometry();
  const plane = new PlaneGeometry(1, 1);
  g.setIndex(plane.getIndex());
  for (const k of ["position", "normal", "uv"]) g.setAttribute(k, plane.getAttribute(k));
  for (const k in attrs) g.setAttribute(k, attrs[k]);
  g.instanceCount = count;
  return g;
}

function plain(g: BufferGeometry, m: Material, order: number): Mesh {
  const mesh = new Mesh(g, m);
  mesh.frustumCulled = false;
  mesh.renderOrder = order;
  mesh.raycast = NO_RAYCAST;
  return mesh;
}

/** All components of one type: one draw call. */
class TypeBatch {
  readonly mesh: InstancedMesh;
  private color: InstancedBufferAttribute;
  private fx: InstancedBufferAttribute;
  private fx2: InstancedBufferAttribute;
  private m = new Matrix4();

  constructor(type: ComponentType, readonly nodes: Node[]) {
    const n = nodes.length;
    const g = archetype(type).clone();
    this.color = attribute(n, 3);
    this.fx = attribute(n, 4);
    this.fx2 = attribute(n, 2);
    g.setAttribute("iColor", this.color);
    g.setAttribute("iFx", this.fx);
    g.setAttribute("iFx2", this.fx2);
    this.mesh = new InstancedMesh(g, componentMaterial(SIZE[type].h), n);
    this.mesh.instanceMatrix.setUsage(DynamicDrawUsage);
    this.mesh.castShadow = this.mesh.receiveShadow = true;
    this.mesh.frustumCulled = false;
    this.mesh.raycast = NO_RAYCAST;
  }

  place(): void {
    this.nodes.forEach((n, i) => {
      const s = n.visible < 0.02 ? 0 : n.scale;
      this.m.makeScale(s, s, s).setPosition(n.x, 0, n.z);
      this.mesh.setMatrixAt(i, this.m);
    });
    this.mesh.instanceMatrix.needsUpdate = true;
  }

  paint(clock: number): void {
    const c = this.color.array as Float32Array;
    const f = this.fx.array as Float32Array;
    const g = this.fx2.array as Float32Array;
    this.nodes.forEach((n, i) => {
      c[i * 3] = n.color.r;
      c[i * 3 + 1] = n.color.g;
      c[i * 3 + 2] = n.color.b;
      f[i * 4] = STATUS_CODE[n.status];
      f[i * 4 + 1] = n.load;
      f[i * 4 + 2] = n.visible;
      f[i * 4 + 3] = n.highlight;
      g[i * 2] = Math.min(100, clock - n.flashAt);
      g[i * 2 + 1] = n.seed;
    });
    this.color.needsUpdate = this.fx.needsUpdate = this.fx2.needsUpdate = true;
  }

  dispose(): void {
    this.mesh.geometry.dispose();
    (this.mesh.material as Material).dispose();
    this.mesh.dispose();
  }
}

const CELLS = 8;
let atlas: CanvasTexture | null = null;

/** All symbols in one texture. */
function iconAtlas(): CanvasTexture {
  if (atlas) return atlas;
  const size = 128;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size * CELLS;
  const ctx = canvas.getContext("2d")!;
  ctx.strokeStyle = "#fff";
  ctx.lineWidth = 2;
  ctx.lineCap = ctx.lineJoin = "round";
  ICON_NAMES.forEach((name, i) => {
    ctx.setTransform(size / 30, 0, 0, size / 30, (i % CELLS) * size + size / 10, Math.floor(i / CELLS) * size + size / 10);
    ctx.stroke(new Path2D(ICONS[name]));
  });
  atlas = new CanvasTexture(canvas);
  atlas.minFilter = LinearMipmapLinearFilter;
  atlas.magFilter = LinearFilter;
  atlas.anisotropy = 4;
  return atlas;
}

const iconIndex = (n: IconName) => ICON_NAMES.indexOf(n);
const SIGN: Partial<Record<Status, IconName>> = {
  warning: "triangle", overloaded: "gauge", failed: "cross", recovering: "cycle", unknown: "question",
};

const linear = (hex: string): [number, number, number] => {
  const c = new Color(hex);
  return [c.r, c.g, c.b];
};
const ANOMALY = linear(AI_COLOR.anomaly);
const RISK = linear(AI_COLOR.risk);

/** Quads per component: plate, warning sign, and the two opinions of the AI. */
const QUADS = 4;

/** Rings on the floor, plates and signs of all components: two draw calls. */
class Marks {
  readonly rings: Mesh;
  readonly plates: Mesh;
  private place_ = attribute(0, 4);
  private color = attribute(0, 3);
  private fx = attribute(0, 4);
  private fx2 = attribute(0, 2);
  private ai = attribute(0, 2);
  private imp = attribute(0, 2);
  private pPos: InstancedBufferAttribute;
  private pColor: InstancedBufferAttribute;
  private pFx: InstancedBufferAttribute;
  private pWhat: InstancedBufferAttribute;

  constructor(readonly nodes: Node[]) {
    const n = nodes.length;
    this.place_ = attribute(n, 4);
    this.color = attribute(n, 3);
    this.fx = attribute(n, 4);
    this.fx2 = attribute(n, 2);
    this.ai = attribute(n, 2);
    this.imp = attribute(n, 2);
    this.rings = plain(quads(n, { iPlace: this.place_, iColor: this.color, iFx: this.fx, iFx2: this.fx2, iAi: this.ai, iImp: this.imp }), ringMaterial(), 2);
    this.pPos = attribute(n * QUADS, 4);
    this.pColor = attribute(n * QUADS, 3);
    this.pFx = attribute(n * QUADS, 4);
    this.pWhat = attribute(n * QUADS, 2);
    this.plates = plain(
      quads(n * QUADS, { iPos: this.pPos, iColor: this.pColor, iFx: this.pFx, iWhat: this.pWhat }),
      plateMaterial({ value: iconAtlas() }), 6);
    nodes.forEach((x, i) => {
      const icon = iconOf({ type: x.type, attributes: { service: x.bankService } });
      this.pWhat.setXY(i * QUADS, iconIndex(icon), 0);
    });
  }

  update(clock: number): void {
    this.nodes.forEach((n, i) => {
      const { r, g, b } = n.color;
      const code = STATUS_CODE[n.status];
      this.place_.setXYZW(i, n.x, n.z, n.size.w / 2, n.size.d / 2);
      this.color.setXYZ(i, r, g, b);
      this.fx.setXYZW(i, code, n.load, n.visible, n.highlight);
      this.fx2.setXY(i, Math.min(100, clock - n.flashAt), n.seed);
      this.ai.setXY(i, n.anomaly, n.risk);
      this.imp.setXY(i, n.impact, n.role);
      const y = n.size.h + 0.62;
      const q = i * QUADS;
      this.pPos.setXYZW(q, n.x, y, n.z, 0.95);
      this.pColor.setXYZ(q, r, g, b);
      this.pFx.setXYZW(q, code, n.visible, n.highlight, n.seed);
      const sign = SIGN[n.status];
      this.pPos.setXYZW(q + 1, n.x, y + 0.92, n.z, sign ? (n.status === "failed" || n.status === "overloaded" ? 0.72 : 0.5) : 0.0001);
      this.pColor.setXYZ(q + 1, r, g, b);
      this.pFx.setXYZW(q + 1, code, sign ? n.visible : 0, n.highlight, n.seed);
      this.pWhat.setXY(q + 1, sign ? iconIndex(sign) : 0, 1);
      // what the AI thinks stands beside the plate, never in place of the status
      this.pPos.setXYZW(q + 2, n.x, y, n.z, 0.72);
      this.pColor.setXYZ(q + 2, ...RISK);
      this.pFx.setXYZW(q + 2, 0, n.risk > 0.02 ? n.visible : 0, n.highlight, n.seed);
      this.pWhat.setXY(q + 2, iconIndex("forecast"), 2);
      this.pPos.setXYZW(q + 3, n.x, y, n.z, 0.72);
      this.pColor.setXYZ(q + 3, ...ANOMALY);
      this.pFx.setXYZW(q + 3, 0, n.anomaly > 0.02 ? n.visible : 0, n.highlight, n.seed);
      this.pWhat.setXY(q + 3, iconIndex("anomaly"), 3);
    });
    for (const a of [this.place_, this.color, this.fx, this.fx2, this.ai, this.imp, this.pPos, this.pColor, this.pFx, this.pWhat]) a.needsUpdate = true;
  }

  dispose(): void {
    for (const m of [this.rings, this.plates]) {
      m.geometry.dispose();
      (m.material as Material).dispose();
    }
  }
}

/** A straight strip of rings: x = position along the link (0..1), y = angle around it. Coarser when
 *  there are so many links that nobody sees one closely. */
function tubeTemplate(links: number): { position: number[]; index: number[] } {
  const [SEGMENTS, SIDES] = links > 400 ? [14, 3] : [28, 5];
  const position: number[] = [];
  const index: number[] = [];
  for (let i = 0; i <= SEGMENTS; i++) {
    for (let k = 0; k <= SIDES; k++) position.push(i / SEGMENTS, (k / SIDES) * Math.PI * 2, 0);
  }
  const row = SIDES + 1;
  for (let i = 0; i < SEGMENTS; i++) {
    for (let k = 0; k < SIDES; k++) {
      const a = i * row + k;
      index.push(a, a + 1, a + row, a + 1, a + row + 1, a + row);
    }
  }
  return { position, index };
}

/** All links and their requests: two draw calls, however many there are. One small texture holds
 *  their state; the shaders do the rest. */
class Links {
  readonly wires: Mesh;
  readonly particles: Mesh;
  private data: Float32Array<ArrayBuffer>;
  private texture: DataTexture;
  private seen = -1;

  constructor(private w: World) {
    const n = Math.max(1, w.wires.length);
    this.data = new Float32Array(new ArrayBuffer(n * LINK_COLS * 16));
    this.texture = new DataTexture(this.data, LINK_COLS, n, RGBAFormat, FloatType);
    this.texture.minFilter = this.texture.magFilter = NearestFilter;
    const u: IUniform = { value: this.texture };
    const t = tubeTemplate(w.wires.length);
    const g = new InstancedBufferGeometry();
    g.setIndex(t.index);
    g.setAttribute("position", new Float32BufferAttribute(t.position, 3));
    g.instanceCount = w.wires.length;
    this.wires = plain(g, wireMaterial(u), 1);
    this.particles = plain(quads(w.wires.length * PER_LINK, {}), particleMaterial(u, w.wires.length), 5);
  }

  update(): void {
    const d = this.data;
    const geometry = this.seen !== this.w.moved;
    this.seen = this.w.moved;
    this.w.wires.forEach((w, i) => {
      const o = i * LINK_COLS * 4;
      if (geometry) {
        for (let c = 0; c < 4; c++) for (let k = 0; k < 3; k++) d[o + c * 4 + k] = w.p[c * 3 + k];
        d[o + 3] = LINK_CODE[w.kind];
        d[o + 19] = w.phase;
      }
      d[o + 7] = w.flow;
      d[o + 11] = w.failing;
      d[o + 15] = w.visible;
      d[o + 16] = w.phase;
      d[o + 17] = STATUS_CODE[w.status];
      d[o + 18] = w.highlight;
      d[o + 19] = (i * 0.618034) % 1;
      const s = LINK_COLORS[w.status];
      d[o + 20] = s[0];
      d[o + 21] = s[1];
      d[o + 22] = s[2];
      d[o + 24] = w.propagation;
      d[o + 25] = w.step;
    });
    this.texture.needsUpdate = true;
  }

  dispose(): void {
    this.texture.dispose();
    for (const m of [this.wires, this.particles]) {
      m.geometry.dispose();
      (m.material as Material).dispose();
    }
  }
}

const LINK_COLORS = Object.fromEntries((Object.keys(STATUS) as Status[]).map((s) => {
  const hex = parseInt(STATUS[s].color.slice(1), 16);
  const lin = (v: number) => Math.pow(v / 255, 2.2);
  return [s, [lin(hex >> 16), lin((hex >> 8) & 255), lin(hex & 255)]];
})) as Record<Status, [number, number, number]>;

/** Everything that depends on the twin. Rebuilt only when components or links appear or vanish. */
export class TwinObjects {
  readonly group = new Group();
  private batches: TypeBatch[] = [];
  private marks: Marks;
  private links: Links;
  private zones: Mesh;
  private zoneShow: InstancedBufferAttribute;
  private moved = -1;
  private shown = "";

  constructor(private w: World) {
    const nodes = [...w.nodes.values()];
    for (const type of COMPONENT_TYPES) {
      const of = nodes.filter((n) => n.type === type);
      if (of.length) this.batches.push(new TypeBatch(type, of));
    }
    this.marks = new Marks(nodes);
    this.links = new Links(w);
    const zs = w.layout.zones;
    const rect = attribute(zs.length, 4);
    this.zoneShow = attribute(zs.length, 1);
    zs.forEach((z, i) => rect.setXYZW(i, z.x0, z.z0, z.x1, z.z1));
    this.zones = plain(quads(zs.length, { iRect: rect, iShow: this.zoneShow }), zoneMaterial(), 1);
    for (const b of this.batches) this.group.add(b.mesh);
    this.group.add(this.zones, this.marks.rings, this.links.wires, this.links.particles, this.marks.plates);
  }

  /** Copies the world to the graphics card. Returns true if shadows must be redrawn. */
  update(zonesVisible: number): boolean {
    let shadows = false;
    let shown = "";
    for (const n of this.w.nodes.values()) shown += n.visible < 0.02 ? "0" : "1";
    if (this.moved !== this.w.moved || shown !== this.shown) {
      this.moved = this.w.moved;
      this.shown = shown;
      shadows = true;
      for (const b of this.batches) b.place();
    }
    for (const b of this.batches) b.paint(this.w.clock);
    this.marks.update(this.w.clock);
    this.links.update();
    for (let i = 0; i < this.zoneShow.count; i++) this.zoneShow.setX(i, zonesVisible);
    this.zoneShow.needsUpdate = true;
    return shadows;
  }

  dispose(): void {
    this.batches.forEach((b) => b.dispose());
    this.marks.dispose();
    this.links.dispose();
    this.zones.geometry.dispose();
    (this.zones.material as Material).dispose();
  }
}
