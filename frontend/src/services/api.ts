import type { PropagationResponse, Topology } from "../types/twin";

const BASE = "/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) throw new Error(`${init?.method ?? "GET"} ${path} -> ${res.status}`);
  return res.json() as Promise<T>;
}

export const api = {
  topology: () => request<Topology>("/topology"),
  analyzePropagation: (failedNodes: string[]) =>
    request<PropagationResponse>("/propagation/analyze", {
      method: "POST",
      body: JSON.stringify({ failed_nodes: failedNodes }),
    }),
};
