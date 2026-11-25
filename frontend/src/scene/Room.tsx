import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import { useThree } from "@react-three/fiber";
import {
  BackSide, Color, DirectionalLight, Fog, InstancedMesh, Matrix4, MeshBasicMaterial, MeshStandardMaterial, PMREMGenerator,
} from "three";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import type { Layout } from "./layout";
import { floorMaterial } from "./shaders";

const BG = "#070b12";

/** The hall. Nothing here stands for a component of the bank; it cannot be selected. */
export function Room({ bounds, shadows }: { bounds: Layout["bounds"]; shadows: boolean }) {
  const { gl, scene } = useThree();
  const floor = useMemo(floorMaterial, []);
  const w = Math.max(30, bounds.x1 - bounds.x0 + 22);
  const d = Math.max(20, bounds.z1 - bounds.z0 + 22);
  const h = 6;
  const cz = (bounds.z0 + bounds.z1) / 2 + 4.5;
  const span = Math.max(w, d);

  useEffect(() => {
    const pmrem = new PMREMGenerator(gl);
    const room = new RoomEnvironment();
    const env = pmrem.fromScene(room, 0.03);
    scene.environment = env.texture;
    scene.environmentIntensity = 0.62;
    scene.background = new Color(BG);
    return () => {
      scene.environment = null;
      env.dispose();
      pmrem.dispose();
    };
  }, [gl, scene]);
  useEffect(() => {
    scene.fog = new Fog(BG, span * 1.6, span * 4.2);
  }, [scene, span]);

  const light = useRef<DirectionalLight>(null);
  useLayoutEffect(() => {
    const l = light.current!;
    const r = span / 2 + 2;
    const c = l.shadow.camera;
    c.left = -r;
    c.right = r;
    c.top = r;
    c.bottom = -r;
    c.near = 1;
    c.far = span * 3;
    c.updateProjectionMatrix();
    gl.shadowMap.needsUpdate = true;
  }, [span, gl, shadows]);

  return (
    <group>
      <hemisphereLight args={["#bcd3f2", "#1a222e", 0.95]} />
      <directionalLight
        ref={light} position={[-span * 0.25, span * 0.75, span * 0.45]} intensity={2.3} color="#e6efff"
        castShadow={shadows} shadow-mapSize={[2048, 2048]} shadow-bias={-0.0004} shadow-normalBias={0.03}
      />
      <directionalLight position={[span * 0.5, span * 0.3, -span * 0.4]} intensity={0.35} color="#7fa8ff" />
      <mesh rotation-x={-Math.PI / 2} position={[0, 0, cz]} receiveShadow material={floor} raycast={() => null}>
        <planeGeometry args={[w, d]} />
      </mesh>
      {/* walls seen from inside only, so they never hide the hall from the camera */}
      <mesh position={[0, h / 2 - 0.02, cz]} raycast={() => null}>
        <boxGeometry args={[w, h, d]} />
        <meshStandardMaterial attach="material-0" color="#27313f" side={BackSide} roughness={0.85} metalness={0.1} />
        <meshStandardMaterial attach="material-1" color="#27313f" side={BackSide} roughness={0.85} metalness={0.1} />
        <meshBasicMaterial attach="material-2" visible={false} />
        <meshBasicMaterial attach="material-3" visible={false} />
        <meshStandardMaterial attach="material-4" color="#232d3a" side={BackSide} roughness={0.85} metalness={0.1} />
        <meshStandardMaterial attach="material-5" color="#232d3a" side={BackSide} roughness={0.85} metalness={0.1} />
      </mesh>
      <Fittings w={w} d={d} h={h} cz={cz} />
    </group>
  );
}

/** Cooling units along the back wall, cable ladder, strip lights: instanced, static. */
function Fittings({ w, d, h, cz }: { w: number; d: number; h: number; cz: number }) {
  const units = Math.max(2, Math.floor(w / 7.5));
  const lamps = Math.max(2, Math.floor(w / 6));
  const rungs = Math.floor(w / 0.6);
  const back = cz - d / 2;
  const mats = useMemo(() => ({
    metal: new MeshStandardMaterial({ color: "#394555", metalness: 0.5, roughness: 0.55 }),
    dark: new MeshStandardMaterial({ color: "#0c1118", metalness: 0.3, roughness: 0.7 }),
    tray: new MeshStandardMaterial({ color: "#3a4656", metalness: 0.75, roughness: 0.45 }),
    lamp: new MeshBasicMaterial({ color: new Color("#a9c9f5").multiplyScalar(0.75) }),
  }), []);
  const at = (n: number, f: (i: number) => [number, number, number]) => Array.from({ length: n }, (_, i) => f(i));
  const ux = (i: number) => -w / 2 + (i + 0.5) * (w / units);
  return (
    <group>
      <Copies at={at(units, (i) => [ux(i), 1.05, back + 0.6])} size={[2.6, 2.1, 0.9]} material={mats.metal} shadow />
      <Copies at={at(units, (i) => [ux(i), 0.75, back + 1.06])} size={[2.2, 1.1, 0.02]} material={mats.dark} />
      <Copies at={at(units, (i) => [ux(i), 1.62, back + 1.06])} size={[0.5, 0.22, 0.02]} material={mats.dark} />
      <Copies at={at(lamps, (i) => [-w / 2 + (i + 0.5) * (w / lamps), h - 0.45, back + 0.08])} size={[2.6, 0.07, 0.06]} material={mats.lamp} />
      <Copies at={[[0, h - 1.25, back + 0.35], [0, h - 1.25, back + 0.95]]} size={[w - 0.4, 0.09, 0.05]} material={mats.tray} />
      <Copies at={at(rungs, (i) => [-w / 2 + 0.3 + i * 0.6, h - 1.25, back + 0.65])} size={[0.05, 0.03, 0.6]} material={mats.tray} />
      <Copies at={at(Math.ceil(d / 5), (i) => [-w / 2 + 0.09, h - 0.45, back + 2.5 + i * 5])} size={[0.06, 0.07, 2.2]} material={mats.lamp} />
      <Copies at={at(Math.ceil(d / 5), (i) => [w / 2 - 0.09, h - 0.45, back + 2.5 + i * 5])} size={[0.06, 0.07, 2.2]} material={mats.lamp} />
    </group>
  );
}

function Copies({ at, size, material, shadow }: {
  at: [number, number, number][];
  size: [number, number, number];
  material: MeshStandardMaterial | MeshBasicMaterial;
  shadow?: boolean;
}) {
  const ref = useRef<InstancedMesh>(null);
  useLayoutEffect(() => {
    const m = new Matrix4();
    at.forEach((p, i) => ref.current!.setMatrixAt(i, m.makeTranslation(...p)));
    ref.current!.instanceMatrix.needsUpdate = true;
  });
  return (
    <instancedMesh
      key={at.length} ref={ref} args={[undefined, undefined, at.length]} material={material} frustumCulled={false}
      castShadow={shadow} receiveShadow={shadow} raycast={() => null}
    >
      <boxGeometry args={size} />
    </instancedMesh>
  );
}
