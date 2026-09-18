import type {
  AgentConnection,
  ApiEnvelope,
  LocalLab,
  LocalRun,
  RunLogEntry,
  ServeResults,
  WiringEntry,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "content-type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  const payload = (await response.json()) as ApiEnvelope<T>;
  if (!response.ok || !payload.ok) {
    throw new Error(payload.error?.message || `Request failed: ${response.status}`);
  }
  return payload.data as T;
}

export type CreateRunBody = {
  parameters?: Record<string, unknown>;
  simulation_config?: Record<string, unknown>;
};

export type UpdateModelBody = {
  alias?: string;
  parameters?: Record<string, unknown>;
};

export type UpdateWorldBody = {
  inputs?: { name: string; maps_to: string }[];
  outputs?: { name: string; maps_to: string }[];
  runtime?: Record<string, unknown>;
  wiring?: WiringEntry[];
};

export type LayoutBody = {
  nodes: Array<{ id: string; position: { x: number; y: number } }>;
};

export const serveApi = {
  lab: () => request<{ lab: LocalLab }>("/api/lab"),
  agent: () => request<{ agent: AgentConnection }>("/api/agent"),
  runs: () => request<{ runs: LocalRun[] }>("/api/runs"),
  createRun: (body: CreateRunBody = {}) =>
    request<{ run: LocalRun }>("/api/runs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  run: (id: string) => request<{ run: LocalRun }>(`/api/runs/${encodeURIComponent(id)}`),
  results: (id: string) =>
    request<{ results: ServeResults }>(`/api/runs/${encodeURIComponent(id)}/results`),
  logs: (id: string, sinceSeq?: number) => {
    const query = sinceSeq != null ? `?since_seq=${encodeURIComponent(String(sinceSeq))}` : "";
    return request<{ logs: RunLogEntry[] }>(`/api/runs/${encodeURIComponent(id)}/logs${query}`);
  },
  cancel: (id: string) =>
    request<{ run: LocalRun; cancelled: boolean }>(
      `/api/runs/${encodeURIComponent(id)}/cancel`,
      { method: "POST", body: "{}" },
    ),

  // Track C — mutation endpoints. Rust handlers added in cli.rs serve_lab_http_response().
  updateModel: (alias: string, body: UpdateModelBody) =>
    request<{ lab: LocalLab }>(`/api/lab/models/${encodeURIComponent(alias)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  updateWorld: (body: UpdateWorldBody) =>
    request<{ lab: LocalLab }>("/api/lab/world", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  saveLayout: (body: LayoutBody) =>
    request<{ lab: LocalLab }>("/api/lab/layout", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
};
