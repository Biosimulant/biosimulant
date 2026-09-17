export type ApiEnvelope<T> = {
  ok: boolean;
  data: T | null;
  error: { message?: string } | null;
};

export type RunStatus =
  | "queued"
  | "pending"
  | "running"
  | "cancelling"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export type LabModelEntry = {
  alias: string;
  path?: string;
  package?: string;
  version?: string;
  parameters?: Record<string, unknown>;
  resolved_model?: {
    title?: string;
    description?: string | null;
    io?: {
      inputs?: Array<{ name: string; description?: string; accepted_units?: string[]; emitted_unit?: string }>;
      outputs?: Array<{ name: string; description?: string; emitted_unit?: string }>;
    } | null;
    biosim?: {
      init_kwargs?: Record<string, unknown>;
      parameters?: Array<{ name: string; value?: number; default?: number; min?: number; max?: number; units?: string; description?: string }>;
    } | null;
    manifest?: {
      parameters?: Array<{ name: string; value?: number; default?: number; min?: number; max?: number; units?: string; description?: string }>;
      [key: string]: unknown;
    } | null;
  } | null;
  resolution_error?: string | null;
};

export type LabChildEntry = {
  alias: string;
  path?: string;
  package?: string;
  version?: string;
  parameters?: Record<string, unknown>;
  resolved_space?: {
    title?: string;
    description?: string | null;
    io?: {
      inputs?: { name: string; maps_to: string }[];
      outputs?: { name: string; maps_to: string }[];
    } | null;
    model_count?: number;
  } | null;
  resolution_error?: string | null;
};

export type WiringEntry = {
  from?: string;
  to?: string | string[];
  source?: string;
  target?: string | string[];
  source_port?: string;
  target_port?: string;
  [key: string]: unknown;
};

export type WorldIoPort = { name: string; maps_to: string };

export type LabRuntime = {
  duration?: number;
  communication_step?: number;
  settle_steps?: number;
  initial_inputs?: Record<string, unknown>;
  [key: string]: unknown;
};

export type ExecutionPolicy = "once_before_run" | "each_window" | "once_after_run";

export type LabExecution = {
  timing: "finite" | "temporal" | "unknown";
  policies: Record<string, ExecutionPolicy | null>;
  undeclared: string[];
};

export type ComputeWarning = {
  code: string;
  message: string;
  model_alias?: string;
  parameter?: string;
  value?: string;
};

export type LocalLab = {
  id: string;
  title: string;
  description: string | null;
  tags: string[];
  file_path?: string | null;
  runtime_metadata_status?: "pending" | "running" | "ready" | "failed";
  runtime_metadata_error?: string | null;
  compute_warnings?: ComputeWarning[];
  execution?: LabExecution;
  manifest: {
    title?: string;
    description?: string;
    models?: LabModelEntry[];
    children?: LabChildEntry[];
    wiring?: WiringEntry[];
    runtime?: LabRuntime;
    io?: {
      inputs?: WorldIoPort[];
      outputs?: WorldIoPort[];
    };
  };
  wiring_layout?: {
    nodes?: Array<{ id: string; position: { x: number; y: number } }>;
  } | null;
  created_at: string;
  updated_at: string;
};

export type LocalRun = {
  id: string;
  lab_id: string | null;
  model_id: string | null;
  status: RunStatus;
  execution_target: "local" | "remote";
  hub_run_id: string | null;
  parameters: Record<string, unknown> | null;
  simulation_config: Record<string, unknown> | null;
  results_summary: Record<string, unknown> | null;
  results_path: string | null;
  error_message: string | null;
  duration_seconds: number | null;
  progress: {
    t?: number | null;
    start?: number | null;
    end?: number | null;
    duration?: number | null;
    remaining?: number | null;
    progress?: number | null;
    progress_pct?: number | null;
  } | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  lab_title?: string;
  model_title?: string;
};

export type RunLogEntry = {
  id: number;
  run_id: string;
  seq: number;
  level: string;
  source: string;
  message: string;
  timestamp: string;
};

export type RunVisualSpec = {
  render: string;
  data: Record<string, unknown>;
  description?: string;
};

export type RunModuleVisuals = {
  module: string;
  module_class?: string;
  visuals: RunVisualSpec[];
};

export type ServeResults = {
  visuals?: RunModuleVisuals[];
  [key: string]: unknown;
};

// UI-side aliases for canvas selection — mirrors compose-canvas-types.ts in the desktop app.
export const WORLD_INPUT_RAIL_ID = "__world_inputs__";
export const WORLD_OUTPUT_RAIL_ID = "__world_outputs__";

export type Selection =
  | { kind: "world" }
  | { kind: "model"; id: string }
  | { kind: "lab"; id: string }
  | { kind: "none" };

export type PreRunPayload = {
  duration?: number;
  communication_step?: number;
  world_inputs: Record<string, unknown>;
  per_model_parameters: Record<string, Record<string, unknown>>;
};
