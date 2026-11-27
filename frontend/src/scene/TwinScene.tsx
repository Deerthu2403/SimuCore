import { useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { HalfFloatType, PerspectiveCamera, Raycaster, Vector2, Vector3, WebGLRenderTarget, type WebGLInfo } from "three";
import { EffectComposer } from "three/examples/jsm/postprocessing/EffectComposer.js";
import { OutputPass } from "three/examples/jsm/postprocessing/OutputPass.js";
import { RenderPass } from "three/examples/jsm/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/examples/jsm/postprocessing/UnrealBloomPass.js";
import { freshness, useTwin } from "../twin/store";
import { currentReach, useView } from "../twin/view";
import { cameraState, CameraRig } from "./CameraRig";
import { LabelMover, Labels, labelState } from "./Labels";
import { TwinObjects } from "./objects";
import { Room } from "./Room";
import { uniforms } from "./shaders";
import { pointOn, world } from "./world";

/** Detail levels, from best to cheapest. */
const LEVELS = [
  { dpr: 2, bloom: true, shadows: true },
  { dpr: 1.25, bloom: true, shadows: true },
  { dpr: 1, bloom: false, shadows: true },
  { dpr: 0.75, bloom: false, shadows: false },
];

// ------------------------------------------------------------------ twin -> world
let synced: unknown = null;
function syncWorld(): void {
  const m = useTwin.getState().mirror;
  if (!m || m === synced) return;
  synced = m;
  world.sync(m);
}
function syncView(): void {
  const v = useView.getState();
  world.view({
    reach: currentReach(), mode: v.mode, hovered: v.hovered, hiddenTypes: v.hiddenTypes,
    selected: v.selected?.kind === "component" ? v.selected.id : null,
    selectedLink: v.selected?.kind === "link" ? v.selected.id : null,
    statusFilter: v.statusFilter, structural: v.structural,
  });
}
// Status filters and dependency reach depend on the data as well as on the view.
useTwin.subscribe((s, old) => {
  if (s.mirror === old.mirror) return;
  syncWorld();
  syncView();
});
useView.subscribe(syncView);

export function usePrefersReducedMotion(): boolean {
  const [reduced, set] = useState(() => matchMedia("(prefers-reduced-motion: reduce)").matches);
  useEffect(() => {
    const q = matchMedia("(prefers-reduced-motion: reduce)");
    const f = () => set(q.matches);
    q.addEventListener("change", f);
    return () => q.removeEventListener("change", f);
  }, []);
  return reduced;
}

export function TwinScene() {
  const reduced = usePrefersReducedMotion();
  const ready = useTwin((s) => !!s.mirror);
  const ids = useTwin((s) => s.mirror?.componentIds);
  const links = useTwin((s) => s.mirror?.linkIds);
  const quality = useView((s) => s.quality);
  const [auto, setAuto] = useState(0);
  const level = quality === "high" ? 0 : quality === "low" ? 3 : auto;
  const spec = LEVELS[level];
  const [lost, setLost] = useState(false);
  if (ready) syncWorld();

  return (
    <div className="scene" data-testid="scene">
      <Canvas
        dpr={Math.min(devicePixelRatio || 1, spec.dpr)} shadows={spec.shadows ? "percentage" : false}
        gl={{ antialias: true, powerPreference: "high-performance", stencil: false }}
        camera={{ fov: 42, near: 0.2, far: 400, position: [-12, 14, 26] }}
        onCreated={({ gl }) => {
          gl.shadowMap.autoUpdate = false;
          gl.shadowMap.needsUpdate = true;
          gl.domElement.addEventListener("webglcontextlost", (e) => {
            e.preventDefault();
            setLost(true);
          });
          gl.domElement.addEventListener("webglcontextrestored", () => setLost(false));
        }}
      >
        {ready && (
          <>
            <Room bounds={world.layout.bounds} shadows={spec.shadows} />
            <Objects key={`${ids?.join()}#${links?.length}#${links?.[links.length - 1]}`} />
            <CameraRig reduced={reduced} />
            <LabelMover />
            <Pointer />
            <Frames reduced={reduced} bloom={spec.bloom} auto={quality === "auto"} level={auto} setLevel={setAuto} />
          </>
        )}
      </Canvas>
      {ready && <Labels />}
      {lost && <div className="scene-note">The graphics card was reset. Restoring the scene…</div>}
    </div>
  );
}

function Objects() {
  const scene = useThree((s) => s.scene);
  const gl = useThree((s) => s.gl);
  const ref = useRef<TwinObjects | null>(null);
  useEffect(() => {
    const o = new TwinObjects(world);
    ref.current = o;
    scene.add(o.group);
    return () => {
      scene.remove(o.group);
      o.dispose();
      ref.current = null;
    };
  }, [scene]);
  useFrame(() => {
    const inNormal = useView.getState().mode === "normal" ? 1 : 0;
    if (ref.current?.update(inNormal)) gl.shadowMap.needsUpdate = true;
  }, -1);
  return null;
}

// ------------------------------------------------------------------ frame loop
const stats = { frames: 0, ms: new Float32Array(240), gaps: new Float32Array(240), calls: 0, triangles: 0, level: 0, bloom: true };

function Frames({ reduced, bloom, auto, level, setLevel }: {
  reduced: boolean; bloom: boolean; auto: boolean; level: number; setLevel: (n: number) => void;
}) {
  const { gl, scene, camera, size } = useThree();
  const dpr = useThree((s) => s.viewport.dpr);
  const composer = useMemo(() => {
    const target = new WebGLRenderTarget(1, 1, { type: HalfFloatType, samples: 4 });
    const c = new EffectComposer(gl, target);
    c.addPass(new RenderPass(scene, camera));
    c.addPass(new UnrealBloomPass(new Vector2(256, 256), 0.5, 0.45, 0.92));
    c.addPass(new OutputPass());
    return c;
  }, [gl, scene, camera]);
  useEffect(() => () => composer.dispose(), [composer]);
  useEffect(() => {
    composer.setPixelRatio(dpr);
    composer.setSize(size.width, size.height);
  }, [composer, size, dpr, bloom]);
  useEffect(() => {
    gl.info.autoReset = false;
    return () => {
      gl.info.autoReset = true;
    };
  }, [gl]);

  const watch = useRef({ slow: 0, count: 0, time: 0, changes: 0 });
  useFrame((_, delta) => {
    const dt = Math.min(delta, 0.1);
    world.step(dt, !reduced);
    uniforms.uTime.value = world.clock;
    uniforms.uMotion.value = reduced ? 0 : 1;
    const current = freshness(useTwin.getState()) === "live" ? 0 : 0.8;
    uniforms.uStale.value += (current - uniforms.uStale.value) * Math.min(1, dt * 4);

    stats.gaps[stats.frames % 240] = delta * 1000;
    stats.level = level;
    stats.bloom = bloom;
    const w = watch.current;
    if (delta < 0.25) {
      w.count++;
      w.time += delta;
    }
    if (auto && w.time > 2.5) {
      w.slow = w.time / w.count > 1 / 42 ? w.slow + 1 : 0;
      w.count = w.time = 0;
      // Only downwards: switching back and forth would be more disturbing than a little less detail.
      if (w.slow >= 2 && level < LEVELS.length - 1) {
        w.slow = 0;
        setLevel(level + 1);
      }
    }
  }, -2);
  useFrame(() => {
    const t0 = performance.now();
    gl.info.reset();
    if (bloom) composer.render();
    else gl.render(scene, camera);
    stats.calls = gl.info.render.calls;
    stats.triangles = gl.info.render.triangles;
    stats.ms[stats.frames % 240] = performance.now() - t0;
    stats.frames++;
  }, 1);
  return null;
}

// ------------------------------------------------------------------ mouse
/** Picks against one box per component: cheap enough for thousands of components. */
function Pointer() {
  const { gl, camera } = useThree();
  useEffect(() => {
    const el = gl.domElement;
    const ray = new Raycaster();
    const ndc = new Vector2();
    const down = { x: 0, y: 0, t: 0, id: -1 };
    const at = (e: PointerEvent | MouseEvent) => {
      const r = el.getBoundingClientRect();
      ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, 1 - ((e.clientY - r.top) / r.height) * 2);
      ray.setFromCamera(ndc, camera);
      return { x: e.clientX - r.left, y: e.clientY - r.top, w: r.width, h: r.height };
    };
    const component = () => world.pick(ray.ray);
    const wire = (p: ReturnType<typeof at>) =>
      world.pickWire(p.x, p.y, (v) => v.project(camera), p.w, p.h);
    let pending: PointerEvent | null = null;
    let raf = 0;
    const hover = () => {
      raf = 0;
      if (!pending) return;
      at(pending);
      const id = component();
      useView.getState().hover(id);
      el.style.cursor = id ? "pointer" : "grab";
    };
    const move = (e: PointerEvent) => {
      if (e.buttons) return; // the camera is being moved
      pending = e;
      if (!raf) raf = requestAnimationFrame(hover);
    };
    const press = (e: PointerEvent) => {
      down.x = e.clientX;
      down.y = e.clientY;
      down.t = performance.now();
      down.id = e.button;
    };
    const release = (e: PointerEvent) => {
      if (e.button !== 0 || down.id !== 0) return;
      if (Math.hypot(e.clientX - down.x, e.clientY - down.y) > 5 || performance.now() - down.t > 700) return;
      const p = at(e);
      const id = component();
      const v = useView.getState();
      if (id) return v.select({ kind: "component", id });
      const k = wire(p);
      if (k && v.mode === "normal") return v.select({ kind: "link", id: k });
      if (v.mode === "normal") v.select(null);
    };
    const open = (e: MouseEvent) => {
      at(e);
      const id = component();
      if (id) useView.getState().select({ kind: "component", id }, { fly: true });
    };
    const leave = () => useView.getState().hover(null);
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerdown", press);
    el.addEventListener("pointerup", release);
    el.addEventListener("dblclick", open);
    el.addEventListener("pointerleave", leave);
    return () => {
      cancelAnimationFrame(raf);
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerdown", press);
      el.removeEventListener("pointerup", release);
      el.removeEventListener("dblclick", open);
      el.removeEventListener("pointerleave", leave);
    };
  }, [gl, camera]);
  useEffect(() => installHook(camera as PerspectiveCamera, gl.domElement, gl.info), [camera, gl]);
  return null;
}

// ------------------------------------------------------------------ for tests
/** Read-only view of what is on screen, so that tests can compare it with the twin. */
function installHook(camera: PerspectiveCamera, el: HTMLCanvasElement, info: WebGLInfo): () => void {
  const v = new Vector3();
  const screen = () => {
    const r = el.getBoundingClientRect();
    v.project(camera);
    return { x: r.left + ((v.x + 1) / 2) * r.width, y: r.top + ((1 - v.y) / 2) * r.height, ahead: v.z < 1 };
  };
  const hook = {
    nodes: () => [...world.nodes.keys()],
    node: (id: string) => {
      const n = world.nodes.get(id);
      if (!n) return null;
      v.set(n.x, n.size.h * 0.55, n.z);
      return {
        status: n.status, color: `#${n.color.getHexString()}`, load: n.load, visible: n.visible,
        highlight: n.highlight, x: n.x, z: n.z, sinceChange: world.clock - n.flashAt, anomaly: n.anomaly, risk: n.risk, screen: screen(),
      };
    },
    wire: (id: string, t = 0.5) => {
      const w = world.wires.find((x) => x.id === id);
      if (!w) return null;
      pointOn(w, t, v);
      return {
        status: w.status, flow: w.flow, failing: w.failing, phase: w.phase, visible: w.visible,
        highlight: w.highlight, screen: screen(),
      };
    },
    wires: () => world.wires.map((w) => w.id),
    /** Which component is in front at a point of the page. */
    pickAt: (x: number, y: number) => {
      const r = el.getBoundingClientRect();
      const ray = new Raycaster();
      ray.setFromCamera(new Vector2(((x - r.left) / r.width) * 2 - 1, 1 - ((y - r.top) / r.height) * 2), camera);
      return world.pick(ray.ray);
    },
    label: labelState,
    camera: cameraState,
    settled: () => world.settled,
    stale: () => uniforms.uStale.value,
    version: () => useTwin.getState().mirror?.meta.version ?? null,
    /** When the twin made the last message and when the page had it (clock of this computer). */
    times: () => ({ made: useTwin.getState().mirror?.meta.generated_at ?? null, received: useTwin.getState().receivedAt }),
    frames: () => ({
      count: stats.frames, memory: { ...info.memory }, calls: stats.calls, triangles: stats.triangles, level: stats.level, bloom: stats.bloom,
      cpuMs: [...stats.ms.slice(0, Math.min(240, stats.frames))], gapMs: [...stats.gaps.slice(0, Math.min(240, stats.frames))],
    }),
  };
  (window as unknown as { __simucore?: typeof hook }).__simucore = hook;
  return () => {
    delete (window as unknown as { __simucore?: typeof hook }).__simucore;
  };
}
