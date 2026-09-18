import type { CompatibilityRecord, RunOutputSignal, ServeResults } from "../types";

export type ScalarOutput = {
  key: string;
  module: string;
  port: string;
  value: number;
  unit: string | null;
  description: string | null;
};

function numericValue(signal: RunOutputSignal): number | null {
  const value = signal?.value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  // A single-sample series still reads as one number to a person.
  if (Array.isArray(value) && value.length === 1 && typeof value[0] === "number") {
    return Number.isFinite(value[0]) ? value[0] : null;
  }
  return null;
}

/** The numbers a person can read at a glance; series and tables stay in the visuals. */
export function scalarOutputs(results: ServeResults | null): ScalarOutput[] {
  const outputs = results?.outputs;
  if (!outputs) return [];
  const scalars: ScalarOutput[] = [];
  for (const [module, ports] of Object.entries(outputs)) {
    if (!ports || typeof ports !== "object") continue;
    for (const [port, signal] of Object.entries(ports)) {
      const value = numericValue(signal);
      if (value === null) continue;
      scalars.push({
        key: `${module}.${port}`,
        module,
        port,
        value,
        unit: signal?.spec?.emitted_unit ?? null,
        description: signal?.spec?.description ?? null,
      });
    }
  }
  return scalars;
}

function formatValue(value: number): string {
  if (Number.isInteger(value)) return String(value);
  if (Math.abs(value) >= 1000 || (Math.abs(value) < 0.001 && value !== 0)) {
    return value.toExponential(2);
  }
  return String(Number(value.toPrecision(3)));
}

function labelFor(output: ScalarOutput): string {
  return output.port.replace(/_/g, " ").replace(/^./, (char) => char.toUpperCase());
}

export function KeyOutputs({ outputs }: { outputs: ScalarOutput[] }) {
  if (!outputs.length) return null;
  return (
    <dl className="key-outputs">
      {outputs.map((output) => (
        <div key={output.key} title={output.description ?? undefined}>
          <dt>{labelFor(output)}</dt>
          <dd>
            {formatValue(output.value)}
            {output.unit ? <span className="key-output-unit">{output.unit}</span> : null}
          </dd>
          <p className="key-output-source">{output.module}</p>
        </div>
      ))}
    </dl>
  );
}

const MODE_LABELS: Record<string, string> = {
  verified: "verified",
  partial: "one-sided",
  structural: "shape only",
  blocked: "blocked",
};

/** What the runtime decided about every wire, as recorded during the run. */
export function CompatibilitySummary({ record }: { record: CompatibilityRecord | null }) {
  const summary = record?.summary;
  if (!summary?.wires) return null;
  const parts = (["verified", "partial", "structural", "blocked"] as const)
    .map((mode) => ({ mode, count: Number(summary[mode] ?? 0) }))
    .filter((part) => part.count > 0);
  if (!parts.length) return null;

  return (
    <div className="compat-summary">
      <h3>Connections</h3>
      <ul>
        {parts.map((part) => (
          <li key={part.mode} className={`compat-${part.mode}`}>
            <span className="compat-dot" aria-hidden />
            {part.count} {MODE_LABELS[part.mode] ?? part.mode}
          </li>
        ))}
      </ul>
      {summary.blocked ? (
        <p className="compat-note">A blocked wire stops the run before results are written.</p>
      ) : null}
    </div>
  );
}

/** Wire key used by the canvas to colour an edge: "module.port->module.port". */
export function compatibilityByWire(record: CompatibilityRecord | null): Record<string, string> {
  const modes: Record<string, string> = {};
  for (const wire of record?.wires ?? []) {
    if (!wire?.source || !wire?.target) continue;
    const key = `${wire.source.module}.${wire.source.port}->${wire.target.module}.${wire.target.port}`;
    modes[key] = wire.mode;
  }
  return modes;
}
