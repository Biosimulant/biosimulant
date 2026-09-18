import * as React from "react";
import { Group as PanelGroup, Panel, Separator as PanelResizeHandle } from "react-resizable-panels";
import "@xyflow/react/dist/style.css";
import "./labs-serve.css";
import { serveApi, type CreateRunBody } from "./api";
import type { AgentConnection, Selection } from "./types";
import { useLab } from "./hooks/use-lab";
import { useRuns } from "./hooks/use-runs";
import { useTheme } from "./hooks/use-theme";
import { Header } from "./components/header";
import { ContentsSidebar } from "./components/contents-sidebar";
import { Canvas } from "./components/canvas";
import { Dock, type DockTab } from "./components/dock";
import { PreRunModal, type PreRunSubmit } from "./components/pre-run-modal";
import { CompareOverlay } from "./components/compare-overlay";
import { compatibilityByWire } from "./components/outcome";

export function App() {
  const [theme, setTheme] = useTheme();
  const labState = useLab();
  const runsState = useRuns();
  const [selection, setSelection] = React.useState<Selection>({ kind: "world" });
  const [contentsOpen, setContentsOpen] = React.useState(true);
  const [dockOpen, setDockOpen] = React.useState(true);
  const [dockTab, setDockTab] = React.useState<DockTab>("results");
  const [showPreRun, setShowPreRun] = React.useState(false);
  const [comparedIds, setComparedIds] = React.useState<Set<string>>(new Set());
  const [showCompare, setShowCompare] = React.useState(false);
  const [agent, setAgent] = React.useState<AgentConnection | null>(null);

  const error = labState.error || runsState.error;

  // The panel group sets its own direction, so a narrow window gives the
  // canvas room by folding the contents rail away rather than by stacking.
  React.useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const narrow = window.matchMedia("(max-width: 900px)");
    const apply = () => setContentsOpen(!narrow.matches);
    apply();
    narrow.addEventListener("change", apply);
    return () => narrow.removeEventListener("change", apply);
  }, []);

  React.useEffect(() => {
    serveApi
      .agent()
      .then(({ agent: connection }) => setAgent(connection))
      .catch(() => setAgent(null));
  }, []);

  // Layout is the one thing this page still writes; everything else is the agent's job.
  async function handleLayoutChange(
    nodes: Array<{ id: string; position: { x: number; y: number } }>,
  ) {
    try {
      const { lab } = await serveApi.saveLayout({ nodes });
      labState.setLab(lab);
    } catch {
      // A layout that fails to save is not worth interrupting anyone over.
    }
  }

  async function handleRunSubmit(payload: PreRunSubmit) {
    setShowPreRun(false);
    setDockTab("log");
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

  const wireModes = React.useMemo(
    () => compatibilityByWire(runsState.results?.compatibility ?? null),
    [runsState.results],
  );

  return (
    <div className="serve-root">
      <Header
        lab={labState.lab}
        activeRun={runsState.activeRun}
        busy={runsState.busy}
        agent={agent}
        onToggleLeft={() => setContentsOpen((open) => !open)}
        onToggleRight={() => setDockOpen((open) => !open)}
        onRefresh={() => {
          void labState.refresh();
          void runsState.refresh();
        }}
        onRunClick={() => setShowPreRun(true)}
        onCancel={() => void runsState.cancelRun()}
        onAgentClick={() => {
          setDockOpen(true);
          setDockTab("agent");
        }}
        theme={theme}
        onThemeChange={setTheme}
      />
      {error ? <div className="error-strip">{error}</div> : null}
      <main className="workbench">
        <PanelGroup orientation="horizontal" id="labs-serve" className="workbench-panels">
          {contentsOpen ? (
            <>
              <Panel defaultSize="14%" minSize="10%" maxSize="24%">
                <ContentsSidebar
                  lab={labState.lab}
                  selection={selection}
                  onSelect={(next) => {
                    setSelection(next);
                    setDockTab("details");
                  }}
                />
              </Panel>
              <PanelResizeHandle className="resize-handle" />
            </>
          ) : null}
          <Panel minSize="35%">
            <Canvas
              lab={labState.lab}
              loading={labState.refreshing && !labState.lab}
              selection={selection}
              wireModes={wireModes}
              onSelect={(next) => {
                setSelection(next);
                if (next.kind !== "none") setDockTab("details");
              }}
              onLayoutChange={handleLayoutChange}
            />
          </Panel>
          {dockOpen ? (
            <>
              <PanelResizeHandle className="resize-handle" />
              <Panel defaultSize="26%" minSize="20%" maxSize="40%">
                <Dock
                  lab={labState.lab}
                  selection={selection}
                  run={runsState.selectedRun}
                  runs={runsState.runs}
                  results={runsState.results}
                  logs={runsState.logs}
                  selectedRunId={runsState.selectedRunId}
                  onSelectRun={(id) => void runsState.refresh(id)}
                  comparedIds={comparedIds}
                  onCompareToggle={toggleCompared}
                  onOpenCompare={() => setShowCompare(true)}
                  agent={agent}
                  tab={dockTab}
                  onTabChange={setDockTab}
                />
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
    </div>
  );
}
