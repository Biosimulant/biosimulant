import * as React from "react";
import { Group as PanelGroup, Panel, Separator as PanelResizeHandle } from "react-resizable-panels";
import "@xyflow/react/dist/style.css";
import "./labs-serve.css";
import { serveApi, type CreateRunBody, type UpdateModelBody, type UpdateWorldBody } from "./api";
import type { LocalLab, Selection } from "./types";
import { useLab } from "./hooks/use-lab";
import { useRuns } from "./hooks/use-runs";
import { useTheme } from "./hooks/use-theme";
import { Header } from "./components/header";
import { ContentsSidebar } from "./components/contents-sidebar";
import { Canvas } from "./components/canvas";
import { Inspector } from "./components/inspector";
import { RunStatus } from "./components/run-status";
import { PreRunModal, type PreRunSubmit } from "./components/pre-run-modal";
import { CompareOverlay } from "./components/compare-overlay";
import { AddToLabModal } from "./components/add-to-lab-modal";

export function App() {
  const [theme, setTheme] = useTheme();
  const labState = useLab();
  const runsState = useRuns();
  const [selection, setSelection] = React.useState<Selection>({ kind: "world" });
  const [leftOpen, setLeftOpen] = React.useState(true);
  const [rightOpen, setRightOpen] = React.useState(true);
  const [showPreRun, setShowPreRun] = React.useState(false);
  const [showAdd, setShowAdd] = React.useState(false);
  const [comparedIds, setComparedIds] = React.useState<Set<string>>(new Set());
  const [showCompare, setShowCompare] = React.useState(false);
  const [saved, setSaved] = React.useState(true);

  // Combined error from either sub-state.
  const error = labState.error || runsState.error;

  function applyLabUpdate(lab: LocalLab) {
    labState.setLab(lab);
    setSaved(true);
  }

  async function handleSaveModel(alias: string, body: UpdateModelBody) {
    setSaved(false);
    try {
      const { lab } = await serveApi.updateModel(alias, body);
      applyLabUpdate(lab);
    } catch (err) {
      // Fall back to a refresh if the endpoint doesn't exist yet (Track C may not be deployed).
      await labState.refresh();
      throw err;
    }
  }

  async function handleSaveWorld(body: UpdateWorldBody) {
    setSaved(false);
    try {
      const { lab } = await serveApi.updateWorld(body);
      applyLabUpdate(lab);
    } catch (err) {
      await labState.refresh();
      throw err;
    }
  }

  async function handleLayoutChange(
    nodes: Array<{ id: string; position: { x: number; y: number } }>,
  ) {
    setSaved(false);
    try {
      const { lab } = await serveApi.saveLayout({ nodes });
      applyLabUpdate(lab);
    } catch {
      // Layout-only failure is non-fatal — the position stays in canvas state until next refresh.
      setSaved(true);
    }
  }

  async function handleRunSubmit(payload: PreRunSubmit) {
    setShowPreRun(false);
    const body: CreateRunBody = {
      parameters: payload.parameters,
      simulation_config: payload.simulation_config,
    };
    try {
      await runsState.startRun(body);
    } catch (err) {
      console.error(err);
    }
  }

  function toggleCompared(id: string) {
    setComparedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const showContents = leftOpen;
  const showRight = rightOpen;
  const showInspector = selection.kind !== "none";

  return (
    <div className="serve-root">
      <Header
        lab={labState.lab}
        activeRun={runsState.activeRun}
        busy={runsState.busy}
        onToggleLeft={() => setLeftOpen((v) => !v)}
        onToggleRight={() => setRightOpen((v) => !v)}
        onRefresh={() => {
          void labState.refresh();
          void runsState.refresh();
        }}
        onRunClick={() => setShowPreRun(true)}
        onCancel={() => void runsState.cancelRun()}
        theme={theme}
        onThemeChange={setTheme}
        saved={saved}
      />
      {error ? <div className="error-strip">{error}</div> : null}
      <LocalModeBanner />
      <main className="workbench">
        <PanelGroup orientation="horizontal" id="labs-serve" className="workbench-panels">
          {showContents ? (
            <>
              <Panel defaultSize="16%" minSize="10%" maxSize="30%">
                <ContentsSidebar
                  lab={labState.lab}
                  selection={selection}
                  onSelect={setSelection}
                />
              </Panel>
              <PanelResizeHandle className="resize-handle" />
            </>
          ) : null}
          <Panel defaultSize={showInspector ? "44%" : "60%"} minSize="30%">
            <Canvas
              lab={labState.lab}
              loading={labState.refreshing && !labState.lab}
              selection={selection}
              onSelect={setSelection}
              onLayoutChange={handleLayoutChange}
              onAddClick={() => setShowAdd(true)}
            />
          </Panel>
          {showInspector ? (
            <>
              <PanelResizeHandle className="resize-handle" />
              <Panel defaultSize="20%" minSize="15%" maxSize="32%">
                <Inspector
                  lab={labState.lab}
                  selection={selection}
                  onClose={() => setSelection({ kind: "none" })}
                  onSaveModel={handleSaveModel}
                  onSaveWorld={handleSaveWorld}
                />
              </Panel>
            </>
          ) : null}
          {showRight ? (
            <>
              <PanelResizeHandle className="resize-handle" />
              <Panel defaultSize="22%" minSize="16%" maxSize="36%">
                <div className="right-stack">
                  <RunStatus
                    run={runsState.selectedRun}
                    results={runsState.results}
                    logs={runsState.logs}
                    runs={runsState.runs}
                    selectedRunId={runsState.selectedRunId}
                    onSelectRun={(id) => void runsState.refresh(id)}
                    comparedIds={comparedIds}
                    onCompareToggle={toggleCompared}
                    onOpenCompare={() => setShowCompare(true)}
                  />
                </div>
              </Panel>
            </>
          ) : null}
        </PanelGroup>
      </main>
      {showPreRun && labState.lab ? (
        <PreRunModal
          lab={labState.lab}
          busy={runsState.busy}
          onCancel={() => setShowPreRun(false)}
          onSubmit={handleRunSubmit}
        />
      ) : null}
      {showCompare ? (
        <CompareOverlay
          runIds={Array.from(comparedIds)}
          onClose={() => setShowCompare(false)}
        />
      ) : null}
      {showAdd ? (
        <AddToLabModal
          labPath={labState.lab?.file_path ?? null}
          onCancel={() => setShowAdd(false)}
        />
      ) : null}
    </div>
  );
}

// Class names still read `upgrade-banner`; they are styling hooks in
// labs-serve.css, not a claim about what the banner says.
export function LocalModeBanner() {
  const [dismissed, setDismissed] = React.useState(false);
  if (dismissed) return null;
  return (
    <div className="upgrade-banner">
      <span className="upgrade-banner-text">
        This lab runs on your machine. Use <code>biosimulant runs create</code> for a
        managed run, or{" "}
        <a
          href="https://docs.biosimulant.com/how-to/agent-gateway"
          target="_blank"
          rel="noreferrer"
        >
          connect Claude or Codex over MCP
        </a>
        .
      </span>
      <button
        type="button"
        className="upgrade-banner-dismiss"
        aria-label="Dismiss"
        onClick={() => setDismissed(true)}
      >
        ×
      </button>
    </div>
  );
}
