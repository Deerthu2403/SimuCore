import { useMemo } from "react";
import { Line } from "@react-three/drei";
import type { DependencyEdge, EdgeKind } from "../types/twin";
import { useTwinStore } from "../store/twinStore";
import type { Vec3 } from "./layout";

const EDGE_COLORS: Record<EdgeKind, string> = {
  calls: "#38bdf8",
  reads_writes: "#a78bfa",
  publishes: "#f472b6",
  hosted_on: "#475569",
  routes_through: "#94a3b8",
};

export function DependencyLines({ edges, positions, visible }: {
  edges: DependencyEdge[];
  positions: Record<string, Vec3>;
  visible: (id: string) => boolean;
}) {
  const selectedId = useTwinStore((s) => s.selectedId);
  const impact = useTwinStore((s) => s.impact);

  // Edges lying on a propagation path (consecutive pairs) are highlighted.
  const impactPairs = useMemo(() => {
    const pairs = new Set<string>();
    impact?.result.impacted.forEach(({ path }) => {
      for (let i = 1; i < path.length; i++) pairs.add(`${path[i]}->${path[i - 1]}`);
    });
    return pairs;
  }, [impact]);

  return (
    <group>
      {edges.map((e) => {
        const a = positions[e.source];
        const b = positions[e.target];
        if (!a || !b || !visible(e.source) || !visible(e.target)) return null;
        const onPath = impactPairs.has(`${e.source}->${e.target}`);
        const touchesSelection = selectedId === e.source || selectedId === e.target;
        const color = onPath ? "#d946ef" : touchesSelection
          ? (selectedId === e.source ? "#22d3ee" : "#a855f7")
          : EDGE_COLORS[e.kind];
        const emphasised = onPath || touchesSelection;
        return (
          <Line
            key={`${e.source}-${e.target}-${e.kind}`}
            points={[a, b]}
            color={color}
            lineWidth={emphasised ? 2.5 : 1}
            transparent
            opacity={emphasised ? 0.95 : 0.15 + e.weight * 0.35}
            dashed={e.kind === "hosted_on"}
            dashSize={0.2}
            gapSize={0.15}
          />
        );
      })}
    </group>
  );
}
