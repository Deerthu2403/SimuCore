import { useMemo } from "react";
import { Canvas } from "@react-three/fiber";
import { Grid, Html, OrbitControls } from "@react-three/drei";
import { useTwinStore } from "../store/twinStore";
import { computeLayout, LAYER_LABELS, LAYER_ORDER, layerY } from "./layout";
import { InfraNodeMesh } from "./InfraNodeMesh";
import { DependencyLines } from "./DependencyLines";

function LayerPlanes() {
  const hidden = useTwinStore((s) => s.hiddenLayers);
  return (
    <>
      {LAYER_ORDER.filter((l) => !hidden.has(l)).map((l) => (
        <group key={l} position={[0, layerY(l) - 0.7, 0]}>
          <mesh rotation={[-Math.PI / 2, 0, 0]}>
            <circleGeometry args={[9.5, 64]} />
            <meshBasicMaterial color="#1e293b" transparent opacity={0.18} />
          </mesh>
          <Html position={[-10.5, 0.1, 0]} center className="layer-label" zIndexRange={[5, 0]}>
            {LAYER_LABELS[l]}
          </Html>
        </group>
      ))}
    </>
  );
}

export function TwinCanvas() {
  const topology = useTwinStore((s) => s.topology);
  const hidden = useTwinStore((s) => s.hiddenLayers);
  const select = useTwinStore((s) => s.select);
  const positions = useMemo(() => (topology ? computeLayout(topology.nodes) : {}), [topology]);
  const layerOf = useMemo(
    () => Object.fromEntries((topology?.nodes ?? []).map((n) => [n.id, n.layer])),
    [topology],
  );
  const visible = (id: string) => !hidden.has(layerOf[id]);

  return (
    <Canvas camera={{ position: [18, 12, 18], fov: 50 }} onPointerMissed={() => select(null)} dpr={[1, 2]}>
      <color attach="background" args={["#0b1120"]} />
      <fog attach="fog" args={["#0b1120", 30, 70]} />
      <ambientLight intensity={0.5} />
      <pointLight position={[10, 20, 10]} intensity={400} />
      <directionalLight position={[-10, 10, -5]} intensity={0.6} />
      <LayerPlanes />
      {topology && (
        <>
          <DependencyLines edges={topology.edges} positions={positions} visible={visible} />
          {topology.nodes.filter((n) => visible(n.id)).map((n) => (
            <InfraNodeMesh key={n.id} node={n} position={positions[n.id]} />
          ))}
        </>
      )}
      <Grid position={[0, -11.5, 0]} args={[60, 60]} cellColor="#1e293b" sectionColor="#334155" fadeDistance={60} />
      <OrbitControls enableDamping makeDefault maxDistance={60} minDistance={6} />
    </Canvas>
  );
}
