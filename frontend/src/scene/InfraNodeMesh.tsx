import { useRef, useState } from "react";
import { useFrame } from "@react-three/fiber";
import { Html } from "@react-three/drei";
import type { Mesh } from "three";
import type { InfraNode } from "../types/twin";
import { STATUS_COLORS, useTwinStore } from "../store/twinStore";
import type { Vec3 } from "./layout";

function Geometry({ kind }: { kind: InfraNode["kind"] }) {
  switch (kind) {
    case "gateway":
      return <octahedronGeometry args={[0.55]} />;
    case "database":
      return <cylinderGeometry args={[0.45, 0.45, 0.8, 24]} />;
    case "cache":
      return <torusGeometry args={[0.35, 0.15, 12, 24]} />;
    case "queue":
      return <boxGeometry args={[1.0, 0.35, 0.35]} />;
    case "server":
      return <boxGeometry args={[0.5, 1.0, 0.5]} />;
    case "load_balancer":
    case "firewall":
    case "switch":
      return <icosahedronGeometry args={[0.45]} />;
    default:
      return <boxGeometry args={[0.7, 0.7, 0.7]} />;
  }
}

export function InfraNodeMesh({ node, position }: { node: InfraNode; position: Vec3 }) {
  const state = useTwinStore((s) => s.states[node.id]);
  const selected = useTwinStore((s) => s.selectedId === node.id);
  const impact = useTwinStore((s) => s.impact?.result.impacted.find((i) => i.node_id === node.id));
  const select = useTwinStore((s) => s.select);
  const [hover, setHover] = useState(false);
  const mesh = useRef<Mesh>(null);

  const status = state?.status ?? "unknown";
  const color = STATUS_COLORS[status];
  const load = Math.min((state?.metrics.cpu_pct ?? 0) / 100, 1);
  const scale = 1 + load * 0.35;

  useFrame(({ clock }) => {
    if (!mesh.current) return;
    const pulse = status === "down" || status === "critical" ? 1 + Math.sin(clock.elapsedTime * 6) * 0.08 : 1;
    mesh.current.scale.setScalar(scale * pulse);
    if (node.kind === "cache" || node.kind === "gateway") mesh.current.rotation.y += 0.01;
  });

  return (
    <group position={position}>
      <mesh
        ref={mesh}
        onClick={(e) => {
          e.stopPropagation();
          select(node.id);
        }}
        onPointerOver={(e) => {
          e.stopPropagation();
          setHover(true);
        }}
        onPointerOut={() => setHover(false)}
      >
        <Geometry kind={node.kind} />
        <meshStandardMaterial
          color={color}
          emissive={impact ? "#d946ef" : color}
          emissiveIntensity={impact ? 0.3 + impact.impact * 0.9 : selected ? 0.9 : 0.35}
          transparent={status === "unknown"}
          opacity={status === "unknown" ? 0.45 : 1}
          roughness={0.35}
          metalness={0.2}
        />
      </mesh>
      {(selected || impact) && (
        <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.6, 0]}>
          <ringGeometry args={[0.7, 0.8 + node.criticality * 0.05, 40]} />
          <meshBasicMaterial color={impact ? "#d946ef" : "#38bdf8"} transparent opacity={0.8} />
        </mesh>
      )}
      {(hover || selected) && (
        <Html distanceFactor={14} position={[0, 1.0, 0]} center className="node-tooltip">
          <strong>{node.name}</strong>
          <span className={`status status-${status}`}>{status}</span>
          {state && state.metrics.cpu_pct === undefined && <small>no metrics reported</small>}
          {state && state.metrics.cpu_pct !== undefined && (
            <small>
              cpu {state.metrics.cpu_pct?.toFixed(0)}% · {state.metrics.latency_ms?.toFixed(1)} ms ·
              err {((state.metrics.error_rate ?? 0) * 100).toFixed(2)}%
            </small>
          )}
        </Html>
      )}
    </group>
  );
}
