import * as React from "react";
import { BracketsCurlyIcon, ChartBarIcon, ClockCounterClockwiseIcon, TreeViewIcon } from "@phosphor-icons/react";
import type { LocalRun, RunLogEntry, ServeResults } from "../types";
import { RunHistoryPanel } from "./run-sidebar";
import { LogsPanel, VisualsPanel } from "./visuals";

type Tab = "visuals" | "logs" | "json" | "history";

function statusClass(status: string | null | undefined) {
  return `status-pill ${String(status || "unknown").toLowerCase()}`;
}

function formatDate(value: string | null | undefined) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function compactJson(value: unknown) {
  if (value == null) return "-";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

function extractProgress(logs: RunLogEntry[], run: LocalRun | null): number {
  if (!run) return 0;
  if (run.status === "completed") return 100;
  const running =
    run.status === "running" ||
    run.status === "queued" ||
    run.status === "pending" ||
    run.status === "cancelling";
  if (run.progress?.progress_pct != null) {
    const progress = Math.min(100, Math.max(0, Number(run.progress.progress_pct)));
    return running ? Math.min(95, progress) : progress;
  }
  const progressLog = [...logs].reverse().find((entry) => /\((\d+(?:\.\d+)?)%\)/.test(entry.message));
  const match = progressLog?.message.match(/\((\d+(?:\.\d+)?)%\)/);
  if (match) {
    const progress = Math.min(100, Math.max(0, Number(match[1])));
    return running ? Math.min(95, progress) : progress;
  }
  return running ? 5 : 0;
}

export type RunStatusProps = {
  run: LocalRun | null;
  results: ServeResults | null;
  logs: RunLogEntry[];
  runs: LocalRun[];
  selectedRunId: string | null;
  onSelectRun: (id: string) => void;
  comparedIds: Set<string>;
  onCompareToggle: (id: string) => void;
  onOpenCompare: () => void;
};

export function RunStatus({
  run,
  results,
  logs,
  runs,
  selectedRunId,
  onSelectRun,
  comparedIds,
  onCompareToggle,
  onOpenCompare,
}: RunStatusProps) {
  const [tab, setTab] = React.useState<Tab>("visuals");
  const progress = extractProgress(logs, run);
  const visuals = Array.isArray(results?.visuals) ? results!.visuals : [];

  return (
    <section className="run-status">
      <div className="run-status-card">
        <div>
          <span className="section-label">Current run</span>
          <h2>{run ? run.id.slice(0, 8) : "No run selected"}</h2>
        </div>
        {run ? <span className={statusClass(run.status)}>{run.status}</span> : null}
        <div className="progress-track">
          <div style={{ width: `${progress}%` }} />
        </div>
        <div className="run-meta">
          <span>Created {formatDate(run?.created_at)}</span>
          <span>{run?.duration_seconds != null ? `${run.duration_seconds.toFixed(1)}s` : "-"}</span>
        </div>
      </div>
      <div className="tabs">
        <button className={tab === "visuals" ? "active" : ""} onClick={() => setTab("visuals")}>
          <ChartBarIcon size={13} /> Visuals
        </button>
        <button className={tab === "logs" ? "active" : ""} onClick={() => setTab("logs")}>
          <TreeViewIcon size={13} /> Logs
        </button>
        <button className={tab === "json" ? "active" : ""} onClick={() => setTab("json")}>
          <BracketsCurlyIcon size={13} /> JSON
        </button>
        <button className={tab === "history" ? "active" : ""} onClick={() => setTab("history")}>
          <ClockCounterClockwiseIcon size={13} /> History
        </button>
      </div>
      <div className="tab-body">
        {tab === "visuals" ? <VisualsPanel visuals={visuals} delivery={results?.visualization} /> : null}
        {tab === "logs" ? <LogsPanel logs={logs} /> : null}
        {tab === "json" ? <pre className="json-block">{compactJson({ run, results })}</pre> : null}
        {tab === "history" ? (
          <RunHistoryPanel
            runs={runs}
            selectedRunId={selectedRunId}
            onSelect={onSelectRun}
            comparedIds={comparedIds}
            onCompareToggle={onCompareToggle}
            onOpenCompare={onOpenCompare}
          />
        ) : null}
      </div>
    </section>
  );
}
