import type { InfraNode, NodeLayer } from "../types/twin";

export const LAYER_ORDER: NodeLayer[] = ["edge", "application", "data", "compute", "network"];
export const LAYER_LABELS: Record<NodeLayer, string> = {
  edge: "Digital channels",
  application: "Banking microservices",
  data: "Data & messaging",
  compute: "Compute hosts",
  network: "Network",
};
export const LAYER_SPACING = 4.5;

/** Layers that actually contain nodes, top to bottom. */
export function presentLayers(nodes: InfraNode[]): NodeLayer[] {
  return LAYER_ORDER.filter((l) => nodes.some((n) => n.layer === l));
}

export function layerY(layer: NodeLayer, present: NodeLayer[]): number {
  return ((present.length - 1) / 2 - present.indexOf(layer)) * LAYER_SPACING;
}

export type Vec3 = [number, number, number];

/** Stacked-ring layout: each layer is a horizontal plane, its nodes on a ring. */
export function computeLayout(nodes: InfraNode[]): Record<string, Vec3> {
  const present = presentLayers(nodes);
  const byLayer = new Map<NodeLayer, InfraNode[]>();
  for (const n of nodes) byLayer.set(n.layer, [...(byLayer.get(n.layer) ?? []), n]);

  const pos: Record<string, Vec3> = {};
  for (const [layer, list] of byLayer) {
    const radius = list.length === 1 ? 0 : Math.max(2.5, list.length * 0.75);
    const offset = LAYER_ORDER.indexOf(layer) * 0.4; // stagger rings so edges don't overlap
    list.forEach((n, i) => {
      const a = (i / list.length) * Math.PI * 2 + offset;
      pos[n.id] = [Math.cos(a) * radius, layerY(layer, present), Math.sin(a) * radius];
    });
  }
  return pos;
}
