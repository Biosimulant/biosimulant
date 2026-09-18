import * as React from "react";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  useStore,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { CircleNotchIcon, FlaskIcon, GitBranchIcon, GlobeIcon, MagicWandIcon } from "@phosphor-icons/react";
import "@xyflow/react/dist/style.css";
import { WORLD_INPUT_RAIL_ID, WORLD_OUTPUT_RAIL_ID, type LocalLab, type Selection } from "../types";
import {
  RAIL_HEADER_HEIGHT,
  RAIL_PILL_GAP,
  RAIL_PILL_HEIGHT,
  RAIL_VERTICAL_PADDING,
  buildGraph,
  railHeight,
  tidyLayout,
  type ModelNodeData,
  type WorldRailData,
} from "../lib/graph";

type HoverHandlers = {
  show: (kind: "Input" | "Output", text: string, event: React.MouseEvent) => void;
  move: (event: React.MouseEvent) => void;
  hide: () => void;
};

const HoverToastContext = React.createContext<HoverHandlers | null>(null);

function usePortHoverProps(kind: "Input" | "Output", text: string) {
  const ctx = React.useContext(HoverToastContext);
  if (!ctx) return {};
  return {
    onMouseEnter: (event: React.MouseEvent) => ctx.show(kind, text, event),
    onMouseMove: (event: React.MouseEvent) => ctx.move(event),
    onMouseLeave: () => ctx.hide(),
  };
}

function PortLabel({ kind, text }: { kind: "Input" | "Output"; text: string }) {
  const hoverProps = usePortHoverProps(kind, text);
  return (
    <span className="flow-port-name" {...hoverProps}>
      {text}
    </span>
  );
}

const PORT_DETAIL_ZOOM = 0.55;

function ModuleNode({ data, id, selected }: NodeProps<Node<ModelNodeData>>) {
  const Icon = data.kind === "lab" ? GitBranchIcon : FlaskIcon;
  // Below this zoom the port names render as unreadable specks, so the node
  // shows what it is and how many ports it has instead.
  const detailed = useStore((state) => state.transform[2] >= PORT_DETAIL_ZOOM);
  const portCount = data.inputs.length + data.outputs.length;
  return (
    <div className={`flow-node ${selected ? "selected" : ""} ${detailed ? "" : "compact"}`} data-node-id={id}>
      <div className="flow-node-top">
        <Icon size={14} />
        <span className="flow-node-title">{data.title}</span>
      </div>
      <div className="flow-node-subtitle">{data.subtitle}</div>
      {!detailed ? (
        <>
          <div className="flow-node-portcount">{portCount} ports</div>
          <Handle type="target" position={Position.Left} className="flow-port-handle" />
          <Handle type="source" position={Position.Right} className="flow-port-handle" />
        </>
      ) : null}
      <div className="flow-node-ports" hidden={!detailed}>
        <div className="flow-port-column">
          <div className="flow-port-column-label">INPUTS</div>
          {data.inputs.length === 0 ? (
            <div className="flow-port-empty">none</div>
          ) : (
            data.inputs.map((port) => (
              <div key={port} className="flow-port input">
                <Handle
                  type="target"
                  position={Position.Left}
                  id={`${data.alias}.${port}`}
                  className="flow-port-handle"
                />
                <PortLabel kind="Input" text={port} />
              </div>
            ))
          )}
        </div>
        <div className="flow-port-column">
          <div className="flow-port-column-label">OUTPUTS</div>
          {data.outputs.length === 0 ? (
            <div className="flow-port-empty">none</div>
          ) : (
            data.outputs.map((port) => (
              <div key={port} className="flow-port output">
                <Handle
                  type="source"
                  position={Position.Right}
                  id={`${data.alias}.${port}`}
                  className="flow-port-handle"
                />
                <PortLabel kind="Output" text={port} />
              </div>
            ))
          )}
        </div>
      </div>
      {data.warning ? <div className="flow-warning">{data.warning}</div> : null}
    </div>
  );
}

function WorldRailNode({
  data,
  selected,
  variant,
}: NodeProps<Node<WorldRailData>> & { variant: "inputs" | "outputs" }) {
  const isInputs = variant === "inputs";
  const handleType: "source" | "target" = isInputs ? "source" : "target";
  const handlePosition = isInputs ? Position.Right : Position.Left;
  const railPrefix = isInputs ? WORLD_INPUT_RAIL_ID : WORLD_OUTPUT_RAIL_ID;
  return (
    <div
      className={`world-rail ${variant} ${selected ? "selected" : ""}`}
      style={{
        width: 220,
        minHeight: railHeight(data.ports.length),
      }}
    >
      <div
        className="world-rail-header"
        style={{ height: RAIL_HEADER_HEIGHT }}
      >
        <GlobeIcon size={13} />
        <span>{isInputs ? "WORLD INPUTS" : "WORLD OUTPUTS"}</span>
      </div>
      <div
        className="world-rail-body"
        style={{ gap: RAIL_PILL_GAP, padding: `${RAIL_VERTICAL_PADDING}px 12px` }}
      >
        {data.ports.length === 0 ? (
          <div className="world-rail-empty">No {variant} defined</div>
        ) : (
          data.ports.map((port) => (
            <div
              key={port}
              className="world-rail-pill"
              style={{ height: RAIL_PILL_HEIGHT }}
            >
              <Handle
                type={handleType}
                position={handlePosition}
                id={`${railPrefix}.${port}`}
                className="world-rail-handle"
              />
              <RailPortLabel variant={variant} text={port} />
            </div>
          ))
        )}
      </div>
    </div>
  );
}

function RailPortLabel({ variant, text }: { variant: "inputs" | "outputs"; text: string }) {
  const hoverProps = usePortHoverProps(variant === "inputs" ? "Input" : "Output", text);
  const className = variant === "inputs" ? "rail-label-right" : "rail-label-left";
  return (
    <span className={className} {...hoverProps}>
      {text}
    </span>
  );
}

const nodeTypes = {
  model: ModuleNode,
  lab: ModuleNode,
  worldInputsRail: (props: NodeProps<Node<WorldRailData>>) => (
    <WorldRailNode {...props} variant="inputs" />
  ),
  worldOutputsRail: (props: NodeProps<Node<WorldRailData>>) => (
    <WorldRailNode {...props} variant="outputs" />
  ),
};

export type CanvasProps = {
  lab: LocalLab | null;
  selection: Selection;
  onSelect: (sel: Selection) => void;
  onLayoutChange?: (nodes: Array<{ id: string; position: { x: number; y: number } }>) => void;
  /** Wire key ("module.port->module.port") to the mode the last run recorded. */
  wireModes?: Record<string, string>;
  readOnly?: boolean;
  loading?: boolean;
};

function CanvasInner({ lab, selection, onSelect, onLayoutChange, wireModes, readOnly, loading }: CanvasProps) {
  const initial = React.useMemo(
    () => (lab ? buildGraph(lab) : { nodes: [] as Node[], edges: [] as Edge[] }),
    [lab],
  );

  const [nodes, setNodes, onNodesChange] = useNodesState(initial.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initial.edges);

  // Re-sync both nodes and edges when the lab payload changes — useEdgesState/useNodesState
  // captures only the initial value, so without these effects the canvas stays empty when the
  // lab loads after first render (which is always the case since the fetch is async).
  React.useEffect(() => {
    setNodes(initial.nodes);
  }, [initial.nodes, setNodes]);
  React.useEffect(() => {
    setEdges(initial.edges);
  }, [initial.edges, setEdges]);

  const handleTidy = React.useCallback(() => {
    setNodes((current) => tidyLayout(current, edges));
  }, [edges, setNodes]);

  const handleNodeDragStop = React.useCallback(
    (_event: React.MouseEvent, node: Node) => {
      if (!onLayoutChange || readOnly) return;
      // Pull the latest snapshot from state.
      setNodes((current) => {
        const positions = current.map((n) => ({ id: n.id, position: n.position }));
        // Ensure the just-dragged node's position is included even if state hasn't flushed.
        const overridden = positions.map((p) =>
          p.id === node.id ? { id: p.id, position: node.position } : p,
        );
        onLayoutChange(overridden);
        return current;
      });
    },
    [onLayoutChange, readOnly, setNodes],
  );

  const handleNodeClick = React.useCallback(
    (_event: React.MouseEvent, node: Node) => {
      if (node.type === "worldInputsRail" || node.type === "worldOutputsRail") {
        onSelect({ kind: "world" });
        return;
      }
      const data = node.data as ModelNodeData | undefined;
      if (data?.kind === "lab") {
        onSelect({ kind: "lab", id: node.id });
      } else {
        onSelect({ kind: "model", id: node.id });
      }
    },
    [onSelect],
  );

  const selectedIds = React.useMemo(() => {
    if (selection.kind === "world") return new Set([WORLD_INPUT_RAIL_ID, WORLD_OUTPUT_RAIL_ID]);
    if (selection.kind === "model" || selection.kind === "lab") return new Set([selection.id]);
    return new Set<string>();
  }, [selection]);

  const decoratedNodes = React.useMemo(
    () => nodes.map((n) => ({ ...n, selected: selectedIds.has(n.id) })),
    [nodes, selectedIds],
  );

  const decoratedEdges = React.useMemo(() => {
    if (!wireModes || !Object.keys(wireModes).length) return edges;
    return edges.map((edge) => {
      const key = `${edge.sourceHandle ?? edge.source}->${edge.targetHandle ?? edge.target}`;
      const mode = wireModes[key];
      return mode ? { ...edge, className: `${edge.className ?? ""} wire-${mode}`.trim() } : edge;
    });
  }, [edges, wireModes]);

  // fitView runs once on mount; the panels around the canvas settle after that,
  // so without this the graph keeps whatever zoom the first layout pass gave it.
  const flow = useReactFlow();
  const paneRef = React.useRef<HTMLDivElement | null>(null);
  React.useEffect(() => {
    const element = paneRef.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => flow.fitView({ padding: 0.12, maxZoom: 1 }));
    });
    observer.observe(element);
    return () => {
      window.cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [flow]);

  const [hoverToast, setHoverToast] = React.useState<{
    kind: "Input" | "Output";
    text: string;
    x: number;
    y: number;
  } | null>(null);

  const hoverHandlers = React.useMemo<HoverHandlers>(
    () => ({
      show: (kind, text, event) => setHoverToast({ kind, text, x: event.clientX, y: event.clientY }),
      move: (event) =>
        setHoverToast((current) => (current ? { ...current, x: event.clientX, y: event.clientY } : current)),
      hide: () => setHoverToast(null),
    }),
    [],
  );

  const runtimeMetadataStatus = lab?.runtime_metadata_status;
  const runtimeMetadataLabel =
    runtimeMetadataStatus === "pending" || runtimeMetadataStatus === "running"
      ? "Preparing runtime metadata..."
      : runtimeMetadataStatus === "failed"
        ? "Runtime metadata unavailable"
        : null;

  return (
    <HoverToastContext.Provider value={hoverHandlers}>
    <div className="serve-canvas" ref={paneRef}>
      <div className="canvas-toolbar">
        <button className="toolbar-button" onClick={handleTidy} title="Auto-arrange nodes">
          <MagicWandIcon size={14} />
          <span>Tidy Layout</span>
        </button>
        <div className="canvas-toolbar-spacer" />
        <span className="canvas-toolbar-stat">{lab?.manifest.models?.length ?? 0} models</span>
        <span className="canvas-toolbar-stat">{lab?.manifest.children?.length ?? 0} nested labs</span>
        <span className="canvas-toolbar-stat">{lab?.manifest.wiring?.length ?? 0} wires</span>
        {runtimeMetadataLabel ? (
          <span
            className={`canvas-toolbar-stat runtime-metadata ${runtimeMetadataStatus}`}
            title={lab?.runtime_metadata_error || runtimeMetadataLabel}
          >
            {runtimeMetadataStatus === "failed" ? null : <CircleNotchIcon size={11} className="spin" />}
            {runtimeMetadataLabel}
          </span>
        ) : null}
      </div>
      {loading ? (
        <div className="empty-state loading-state">
          <CircleNotchIcon size={28} className="spin" />
          <h2>Loading lab...</h2>
          <p>Reading the local lab manifest and canvas layout.</p>
        </div>
      ) : decoratedNodes.length === 0 ? (
        <div className="empty-state">
          <FlaskIcon size={28} />
          <h2>No modules in this lab</h2>
          <p>This local lab has no model or nested lab entries to draw.</p>
        </div>
      ) : (
        <ReactFlow
          nodes={decoratedNodes}
          edges={decoratedEdges}
          nodeTypes={nodeTypes}
          fitView
          fitViewOptions={{ padding: 0.12, maxZoom: 1 }}
          minZoom={0.35}
          maxZoom={1.6}
          nodesDraggable={!readOnly}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeClick={handleNodeClick}
          onNodeDragStop={handleNodeDragStop}
          onPaneClick={() => onSelect({ kind: "world" })}
          proOptions={{ hideAttribution: true }}
        >
          <Background />
          <Controls showInteractive={false} />
        </ReactFlow>
      )}
      {hoverToast ? <PortHoverToast toast={hoverToast} /> : null}
    </div>
    </HoverToastContext.Provider>
  );
}

function PortHoverToast({
  toast,
}: {
  toast: { kind: "Input" | "Output"; text: string; x: number; y: number };
}) {
  // Position near the cursor but clamped to the viewport so the toast never gets clipped.
  const offset = 14;
  const width = 320;
  const margin = 12;
  const viewportWidth = typeof window === "undefined" ? 1024 : window.innerWidth || 1024;
  const left = Math.min(toast.x + offset, Math.max(margin, viewportWidth - width - margin));
  const top = Math.max(margin, toast.y + offset);
  return (
    <div className="port-hover-toast" style={{ left, top, maxWidth: width }}>
      <div className="port-hover-toast-kind">{toast.kind}</div>
      <div className="port-hover-toast-text">{toast.text}</div>
    </div>
  );
}

export function Canvas(props: CanvasProps) {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  );
}
