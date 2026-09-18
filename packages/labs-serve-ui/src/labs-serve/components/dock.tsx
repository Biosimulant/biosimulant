import * as React from "react";
import type { AgentConnection, LocalLab, LocalRun, RunLogEntry, Selection, ServeResults } from "../types";
import { isActive } from "../hooks/use-runs";
import { AgentPanel } from "./agent-panel";
import { Inspector } from "./inspector";
import { CompatibilitySummary, KeyOutputs, scalarOutputs } from "./outcome";
import { RunHistoryPanel } from "./run-sidebar";
import { LogsPanel, VisualsPanel } from "./visuals";

export type DockTab = "results" | "log" | "runs" | "details" | "agent";

const TAB_LABELS: Array<[DockTab, string]> = [
  ["results", "Results"],
  ["log", "Log"],
  ["runs", "Runs"],
  ["details", "Details"],
  ["agent", "Agent"],
];

function statusClass(status: string | null | undefined) {
  return `status-pill ${String(status || "unknown").toLowerCase()}`;
}

function formatWhen(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

/** A run only knows a real percentage when it reports one; the rest is honest waiting. */
function RunHeadline({ run }: { run: LocalRun | null }) {
  if (!run) {
    return (
      <div className="dock-run">
        <p className="muted">No runs yet. Start one, or ask your agent to.</p>
      </div>
    );
  }
  const active = isActive(run);
  const reported = run.progress?.progress_pct;
  const pct = typeof reported === "number" && Number.isFinite(reported)
    ? Math.max(0, Math.min(100, Math.round(reported)))
    : null;

  return (
    <div className="dock-run">
      <div className="dock-run-head">
        <h2>{run.id.slice(0, 12)}</h2>
        <span className={statusClass(run.status)}>{run.status}</span>
      </div>
      <p className="dock-run-meta">
        {formatWhen(run.created_at)}
        {run.duration_seconds != null ? ` · ${run.duration_seconds.toFixed(1)}s` : ""}
      </p>
      {active ? (
        <div className={`progress-track ${pct === null ? "indeterminate" : ""}`}>
          <div style={pct === null ? undefined : { width: `${pct}%` }} />
        </div>
      ) : null}
    </div>
  );
}

export type DockProps = {
  lab: LocalLab | null;
  selection: Selection;
  run: LocalRun | null;
  runs: LocalRun[];
  results: ServeResults | null;
  logs: RunLogEntry[];
  selectedRunId: string | null;
  onSelectRun: (id: string) => void;
  comparedIds: Set<string>;
  onCompareToggle: (id: string) => void;
  onOpenCompare: () => void;
  agent: AgentConnection | null;
  tab: DockTab;
  onTabChange: (tab: DockTab) => void;
};

export function Dock(props: DockProps) {
  const { lab, selection, run, runs, results, logs, agent, tab, onTabChange } = props;
  const outputs = React.useMemo(() => scalarOutputs(results), [results]);
  const compatibility = results?.compatibility ?? null;
  const visuals = Array.isArray(results?.visuals) ? results!.visuals! : [];

  return (
    <section className="dock">
      <RunHeadline run={run} />
      <div className="dock-tabs" role="tablist" aria-label="Lab panels">
        {TAB_LABELS.map(([id, label]) => (
          <button
            key={id}
            role="tab"
            id={`dock-tab-${id}`}
            aria-selected={tab === id}
            aria-controls={`dock-panel-${id}`}
            className={tab === id ? "active" : ""}
            onClick={() => onTabChange(id)}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="dock-body" id={`dock-panel-${tab}`} role="tabpanel" aria-labelledby={`dock-tab-${tab}`}>
        {tab === "results" ? (
          <>
            <KeyOutputs outputs={outputs} />
            <CompatibilitySummary record={compatibility} />
            <VisualsPanel visuals={visuals} />
          </>
        ) : null}
        {tab === "log" ? <LogsPanel logs={logs} /> : null}
        {tab === "runs" ? (
          <RunHistoryPanel
            runs={runs}
            selectedRunId={props.selectedRunId}
            onSelect={props.onSelectRun}
            comparedIds={props.comparedIds}
            onCompareToggle={props.onCompareToggle}
            onOpenCompare={props.onOpenCompare}
          />
        ) : null}
        {/* No save handlers: the agent edits the lab, this page shows what it did. */}
        {tab === "details" ? (
          <Inspector lab={lab} selection={selection} onClose={() => undefined} />
        ) : null}
        {tab === "agent" ? <AgentPanel connection={agent} /> : null}
      </div>
    </section>
  );
}
