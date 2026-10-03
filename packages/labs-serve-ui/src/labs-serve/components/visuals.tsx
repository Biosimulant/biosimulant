import * as React from "react";
import { ArrowsOutIcon, GitBranchIcon } from "@phosphor-icons/react";
import "molstar/build/viewer/molstar.css";
import type { Viewer as MolstarViewerInstance } from "molstar/lib/apps/viewer/app";
import type { RunModuleVisuals, RunVisualSpec } from "../types";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function compactJson(value: unknown): string {
  if (value == null) return "-";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

function visualTitle(visual: RunVisualSpec): string {
  const title = visual.data?.title;
  return typeof title === "string" && title.trim() ? title : visual.render;
}

export function VisualsPanel({ visuals, delivery }: { visuals: RunModuleVisuals[]; delivery?: unknown }) {
  const [expanded, setExpanded] = React.useState<{ title: string; visual: RunVisualSpec } | null>(null);
  const report = isRecord(delivery) ? delivery : null;
  const notice = report && (report.status === "failed" || (Array.isArray(report.diagnostics) && report.diagnostics.length > 0))
    ? <p role="status">Some visualizations are missing or invalid. Review the results JSON diagnostics before using or publishing these results.</p>
    : null;
  if (!visuals.length) return <>{notice}<div className="empty-card">Run visuals will appear here after results are available.</div></>;
  return (
    <>
      {notice}
      <div className="visual-stack">
        {visuals.map((module) => (
          <section key={module.module} className="visual-module">
            <div className="visual-module-header">
              <h3>{module.module}</h3>
              {module.module_class ? <span>{module.module_class}</span> : null}
            </div>
            {module.visuals.map((visual, index) => (
              <article key={`${module.module}-${index}`} className="visual-card">
                <div className="visual-card-header">
                  <div className="visual-card-title">
                    <h4>{visualTitle(visual)}</h4>
                    <span className="renderer-badge">{visual.render}</span>
                  </div>
                  <button className="icon-button small" onClick={() => setExpanded({ title: module.module, visual })}>
                    <ArrowsOutIcon size={12} />
                  </button>
                </div>
                {visual.description ? <p>{visual.description}</p> : null}
                <VisualRenderer visual={visual} />
              </article>
            ))}
          </section>
        ))}
      </div>
      {expanded ? (
        <div className="modal-backdrop" onClick={() => setExpanded(null)}>
          <div className="modal large" onClick={(event) => event.stopPropagation()}>
            <div className="modal-header">
              <h2>{expanded.title}</h2>
              <button className="icon-button" onClick={() => setExpanded(null)}>
                ×
              </button>
            </div>
            <VisualRenderer visual={expanded.visual} expanded />
          </div>
        </div>
      ) : null}
    </>
  );
}

export function VisualRenderer({ visual, expanded = false }: { visual: RunVisualSpec; expanded?: boolean }) {
  const render = visual.render.toLowerCase();
  const renderer = VISUAL_RENDERERS[render];
  if (renderer) return renderer({ data: visual.data, expanded });
  return <UnsupportedVisual render={visual.render} data={visual.data} />;
}

type VisualRendererFn = (props: { data: Record<string, unknown>; expanded: boolean }) => React.ReactNode;

const VISUAL_RENDERERS: Record<string, VisualRendererFn> = {
  table: ({ data }) => <TableVisual data={data} />,
  image: ({ data }) => <ImageVisual data={data} />,
  timeseries: ({ data, expanded }) => <SeriesVisual data={data} expanded={expanded} />,
  line: ({ data, expanded }) => <SeriesVisual data={data} expanded={expanded} />,
  bar: ({ data }) => <BarVisual data={data} />,
  scatter: ({ data }) => <ScatterVisual data={data} />,
  heatmap: ({ data }) => <HeatmapVisual data={data} />,
  graph: ({ data }) => <GraphVisual data={data} />,
  text: ({ data }) => <TextVisual data={data} />,
  json: ({ data }) => <JsonVisual data={data} />,
  structure3d: ({ data, expanded }) => <Structure3DVisual data={data} expanded={expanded} />,
};

function TableVisual({ data }: { data: Record<string, unknown> }) {
  // Accept three row shapes: array-row (`["Metric A", 1.0]`), record-row (`{Metric: "A", Value: 1}`),
  // and items-array (`{items: [{...}]}`) — same coverage as the desktop run-visuals.tsx table.
  const items = Array.isArray(data.items) ? data.items : [];
  const rowsRaw = Array.isArray(data.rows) ? data.rows : items;
  const columns = Array.isArray(data.columns)
    ? data.columns.map(String)
    : rowsRaw[0] && isRecord(rowsRaw[0])
      ? Object.keys(rowsRaw[0] as Record<string, unknown>)
      : [];
  if (!columns.length) return <pre className="json-block compact">{compactJson(data)}</pre>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rowsRaw.slice(0, 50).map((row, index) => (
            <tr key={index}>
              {columns.map((column, colIndex) => {
                let cell: unknown = "";
                if (Array.isArray(row)) cell = row[colIndex];
                else if (isRecord(row)) cell = row[column];
                return <td key={column}>{cell == null ? "" : String(cell)}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ImageVisual({ data }: { data: Record<string, unknown> }) {
  const source = isRecord(data.source) ? data.source : {};
  const src =
    typeof source.url === "string" ? source.url : typeof data.url === "string" ? data.url : typeof data.src === "string" ? data.src : undefined;
  if (!src) return <pre className="json-block compact">{compactJson(data)}</pre>;
  return <figure>
    <img className="image-visual" src={src} alt={typeof data.alt === "string" ? data.alt : "visual"} />
    {typeof data.caption === "string" ? <figcaption>{data.caption}</figcaption> : null}
  </figure>;
}

function TextVisual({ data }: { data: Record<string, unknown> }) {
  const text = typeof data.text === "string" ? data.text : typeof data.markdown === "string" ? data.markdown : typeof data.value === "string" ? data.value : compactJson(data);
  return <div className="text-visual">{text}</div>;
}

function JsonVisual({ data }: { data: Record<string, unknown> }) {
  return <pre className="json-block compact">{compactJson(data)}</pre>;
}

function UnsupportedVisual({ render, data }: { render: string; data: Record<string, unknown> }) {
  return (
    <div className="visual-fallback">
      <strong>Unsupported renderer: {render}</strong>
      <pre className="json-block compact">{compactJson(data)}</pre>
    </div>
  );
}

export function getSeries(data: Record<string, unknown>) {
  const series = Array.isArray(data.series) ? data.series : [];
  return series.flatMap((entry) => {
    if (!isRecord(entry) || !Array.isArray(entry.points)) return [];
    return [
      {
        name: typeof entry.name === "string" ? entry.name : "series",
        points: entry.points.filter(Array.isArray) as unknown[][],
      },
    ];
  });
}

function formatTick(value: number): string {
  if (!Number.isFinite(value)) return "";
  const abs = Math.abs(value);
  if (abs !== 0 && (abs < 0.01 || abs >= 10000)) return value.toExponential(1);
  if (Number.isInteger(value)) return value.toString();
  return value.toFixed(abs < 1 ? 3 : abs < 10 ? 2 : 1);
}

function ticks(min: number, max: number, count: number): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) return [min];
  return Array.from({ length: count + 1 }, (_, index) => min + ((max - min) * index) / count);
}

function axisTitle(data: Record<string, unknown>, axis: "x" | "y") {
  const label = data[`${axis}_label`] ?? data[`${axis}label`];
  const unit = data[`${axis}_unit`];
  return [typeof label === "string" ? label : "", typeof unit === "string" && unit ? `(${unit})` : ""].filter(Boolean).join(" ");
}

function SeriesVisual({ data, expanded }: { data: Record<string, unknown>; expanded: boolean }) {
  const series = getSeries(data);
  if (!series.length) return <pre className="json-block compact">{compactJson(data)}</pre>;
  const points = series.flatMap((entry) =>
    entry.points.map((point) => [Number(point[0]), Number(point[1])] as const),
  );
  const xs = points.map((point) => point[0]).filter(Number.isFinite);
  const ys = points.map((point) => point[1]).filter(Number.isFinite);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const width = 520;
  const height = expanded ? 360 : 200;
  const marginLeft = 44;
  const marginRight = 12;
  const marginTop = 10;
  const marginBottom = axisTitle(data, "x") ? 40 : 22;
  const plotW = width - marginLeft - marginRight;
  const plotH = height - marginTop - marginBottom;
  const xSpan = Math.max(1e-9, maxX - minX);
  const ySpan = Math.max(1e-9, maxY - minY);
  const toX = (x: number) => marginLeft + ((x - minX) / xSpan) * plotW;
  const toY = (y: number) => marginTop + (1 - (y - minY) / ySpan) * plotH;
  const xTicks = ticks(minX, maxX, 4);
  const yTicks = ticks(minY, maxY, 4);
  return (
    <svg className="chart" viewBox={`0 0 ${width} ${height}`}>
      {/* horizontal grid + Y-axis ticks */}
      {yTicks.map((tick, i) => (
        <g key={`y-${i}`}>
          <line className="grid-line" x1={marginLeft} y1={toY(tick)} x2={width - marginRight} y2={toY(tick)} />
          <text className="axis-label" x={marginLeft - 6} y={toY(tick) + 3} textAnchor="end">
            {formatTick(tick)}
          </text>
        </g>
      ))}
      {/* vertical grid + X-axis ticks */}
      {xTicks.map((tick, i) => (
        <g key={`x-${i}`}>
          <line className="grid-line" x1={toX(tick)} y1={marginTop} x2={toX(tick)} y2={height - marginBottom} />
          <text className="axis-label" x={toX(tick)} y={height - 8} textAnchor="middle">
            {formatTick(tick)}
          </text>
        </g>
      ))}
      {/* axes */}
      <line className="axis-line" x1={marginLeft} y1={marginTop} x2={marginLeft} y2={height - marginBottom} />
      <line className="axis-line" x1={marginLeft} y1={height - marginBottom} x2={width - marginRight} y2={height - marginBottom} />
      {axisTitle(data, "x") ? <text className="axis-label" x={width / 2} y={height - 2} textAnchor="middle">{axisTitle(data, "x")}</text> : null}
      {axisTitle(data, "y") ? <text className="axis-label" transform={`translate(10 ${height / 2}) rotate(-90)`} textAnchor="middle">{axisTitle(data, "y")}</text> : null}
      {/* data series */}
      {series.map((entry, index) => (
        <polyline
          key={entry.name}
          fill="none"
          stroke={`var(--chart-${(index % 5) + 1})`}
          strokeWidth="2"
          points={entry.points
            .map((point) => `${toX(Number(point[0]))},${toY(Number(point[1]))}`)
            .join(" ")}
        />
      ))}
    </svg>
  );
}

function BarVisual({ data }: { data: Record<string, unknown> }) {
  const items = Array.isArray(data.items) ? data.items : Array.isArray(data.bars) ? data.bars : [];
  const parsed = items.flatMap((item) =>
    isRecord(item) ? [{ label: String(item.label ?? ""), value: Number(item.value ?? 0), unit: typeof item.unit === "string" ? item.unit : "" }] : [],
  );
  const max = Math.max(1, ...parsed.map((item) => Math.abs(item.value)));
  if (!parsed.length) return <pre className="json-block compact">{compactJson(data)}</pre>;
  return (
    <div className="bar-list">
      {parsed.map((item) => (
        <div key={item.label} className="bar-row">
          <span>{item.label}</span>
          <div style={{ position: "relative" }}>
            <i style={{ position: "absolute", left: `${item.value < 0 ? 50 - Math.abs(item.value) / max * 50 : 50}%`, width: `${Math.abs(item.value) / max * 50}%` }} />
          </div>
          <b>{item.value} {item.unit}</b>
        </div>
      ))}
    </div>
  );
}

function ScatterVisual({ data }: { data: Record<string, unknown> }) {
  const points = Array.isArray(data.points)
    ? data.points.flatMap((point) =>
        isRecord(point) ? [{ x: Number(point.x), y: Number(point.y) }] : [],
      )
    : [];
  if (!points.length) return <pre className="json-block compact">{compactJson(data)}</pre>;
  const xs = points.map((point) => point.x);
  const ys = points.map((point) => point.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  return (
    <svg className="chart" viewBox="0 0 360 205" role="img" aria-label="Scatter plot">
      <text className="axis-label" x="180" y="199" textAnchor="middle">{axisTitle(data, "x")}</text>
      <text className="axis-label" transform="translate(10 90) rotate(-90)" textAnchor="middle">{axisTitle(data, "y")}</text>
      {points.map((point, index) => (
        <circle
          key={index}
          cx={18 + ((point.x - minX) / Math.max(1, maxX - minX)) * 324}
          cy={162 - ((point.y - minY) / Math.max(1, maxY - minY)) * 144}
          r="3"
        />
      ))}
    </svg>
  );
}

function HeatmapVisual({ data }: { data: Record<string, unknown> }) {
  const matrix = Array.isArray(data.values) ? data.values : Array.isArray(data.matrix) ? data.matrix : [];
  const rows = matrix.filter(Array.isArray) as number[][];
  if (!rows.length || !rows[0]?.length) return <pre className="json-block compact">{compactJson(data)}</pre>;
  const values = rows.flat().map(Number).filter(Number.isFinite);
  const min = Math.min(...values), max = Math.max(...values);
  const xLabels = Array.isArray(data.x_labels) ? data.x_labels : [];
  const yLabels = Array.isArray(data.y_labels) ? data.y_labels : [];
  const unit = typeof data.unit === "string" ? data.unit : "";
  return <div className="heatmap" style={{ display: "block", overflow: "auto" }}>
    <table aria-label={typeof data.title === "string" ? data.title : "Heatmap"}>
      <thead><tr><th>{unit}</th>{rows[0].map((_, col) => <th key={col}>{String(xLabels[col] ?? col + 1)}</th>)}</tr></thead>
      <tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>
        <th>{String(yLabels[rowIndex] ?? rowIndex + 1)}</th>
        {row.map((raw, col) => { const value = Number(raw); const ratio = max === min ? .5 : (value - min) / (max - min); return <td key={col} title={`${value} ${unit}`} style={{ padding: "8px 12px", background: `hsl(${220 - ratio * 220}, 72%, ${94 - ratio * 48}%)`, color: ratio > .65 ? "white" : "#172033" }}>{formatTick(value)}</td>; })}
      </tr>)}</tbody>
    </table>
  </div>;
}

function GraphVisual({ data }: { data: Record<string, unknown> }) {
  const nodes = (Array.isArray(data.nodes) ? data.nodes : []).filter(isRecord);
  const edges = (Array.isArray(data.edges) ? data.edges : []).filter(isRecord);
  const positions = new Map(nodes.map((node, index) => {
    const angle = 2 * Math.PI * index / Math.max(1, nodes.length);
    return [String(node.id), { x: 260 + 150 * Math.cos(angle), y: 140 + 90 * Math.sin(angle) }] as const;
  }));
  return <div className="graph-visual">
    <div className="graph-summary"><GitBranchIcon size={14} /> {nodes.length} nodes / {edges.length} edges</div>
    <svg className="chart" viewBox="0 0 520 280" role="img" aria-label={typeof data.title === "string" ? data.title : "Network"}>
      {edges.map((edge, index) => { const from = positions.get(String(edge.source)); const to = positions.get(String(edge.target)); return from && to ? <line key={index} x1={from.x} y1={from.y} x2={to.x} y2={to.y} stroke="currentColor" opacity=".5"><title>{String(edge.source)} → {String(edge.target)}</title></line> : null; })}
      {nodes.map((node) => { const position = positions.get(String(node.id))!; return <g key={String(node.id)}>
        <circle cx={position.x} cy={position.y} r="10" fill="#0f766e" />
        <text x={position.x} y={position.y + 25} textAnchor="middle" fill="currentColor" fontSize="11">{String(node.label ?? node.id)}</text>
      </g>; })}
    </svg>
  </div>;
}

type StructureFormat = "pdb" | "mmcif";
type MolstarFormat = Parameters<MolstarViewerInstance["loadStructureFromUrl"]>[1];

function normalizeStructureFormat(value: unknown): StructureFormat | null {
  if (typeof value !== "string") return null;
  const format = value.trim().toLowerCase();
  if (format === "pdb") return "pdb";
  if (format === "mmcif" || format === "cif") return "mmcif";
  return null;
}

function structureSourceUrl(data: Record<string, unknown>): string | null {
  const source = data.source;
  if (isRecord(source)) {
    const url = typeof source.url === "string" ? source.url : typeof source.src === "string" ? source.src : null;
    if (url && url.trim()) return url;
  }
  const url = typeof data.url === "string" ? data.url : typeof data.src === "string" ? data.src : null;
  return url && url.trim() ? url : null;
}

function structureAnnotations(data: Record<string, unknown>) {
  const annotations = Array.isArray(data.annotations) ? data.annotations : [];
  return annotations.flatMap((entry) => {
    if (!isRecord(entry)) return [];
    const label = typeof entry.label === "string" ? entry.label : null;
    if (!label) return [];
    return [{ label, value: entry.value == null ? "-" : String(entry.value) }];
  });
}

function Structure3DVisual({ data, expanded }: { data: Record<string, unknown>; expanded: boolean }) {
  const containerRef = React.useRef<HTMLDivElement | null>(null);
  const [state, setState] = React.useState<"idle" | "loading" | "ready" | "error">("idle");
  const [error, setError] = React.useState<string | null>(null);
  const url = structureSourceUrl(data);
  const format = normalizeStructureFormat(data.format);
  const annotations = structureAnnotations(data);

  React.useEffect(() => {
    const element = containerRef.current;
    if (!element || !url || !format) return;
    let disposed = false;
    let viewer: MolstarViewerInstance | null = null;
    setState("loading");
    setError(null);
    import("molstar/lib/apps/viewer/app")
      .then(async ({ Viewer }) => {
        if (disposed) return;
        viewer = await Viewer.create(element, {
          layoutShowControls: expanded,
          layoutShowSequence: expanded,
          viewportShowExpand: false,
          collapseLeftPanel: !expanded,
        });
        if (disposed) {
          viewer.dispose();
          return;
        }
        await viewer.loadStructureFromUrl(url, format as MolstarFormat, false, {
          label: typeof data.title === "string" ? data.title : undefined,
        });
        if (!disposed) setState("ready");
      })
      .catch((exc: unknown) => {
        if (disposed) return;
        setState("error");
        setError(exc instanceof Error ? exc.message : String(exc));
      });
    return () => {
      disposed = true;
      viewer?.dispose();
    };
  }, [data.title, expanded, format, url]);

  if (!url) return <div className="visual-fallback">Structure artifact URL is missing.</div>;
  if (!format) return <div className="visual-fallback">Unsupported structure format.</div>;

  return (
    <div className={`structure-visual ${expanded ? "expanded" : ""}`}>
      <div ref={containerRef} className="structure-viewport" />
      {state === "loading" || state === "idle" ? <div className="structure-overlay">Loading structure...</div> : null}
      {state === "error" ? <div className="structure-overlay error">Could not load structure: {error}</div> : null}
      {annotations.length ? (
        <dl className="structure-annotations">
          {annotations.map((annotation) => (
            <React.Fragment key={annotation.label}>
              <dt>{annotation.label}</dt>
              <dd>{annotation.value}</dd>
            </React.Fragment>
          ))}
        </dl>
      ) : null}
    </div>
  );
}

export function LogsPanel({ logs }: { logs: import("../types").RunLogEntry[] }) {
  if (!logs.length) return <div className="empty-card">No logs for this run.</div>;
  return (
    <div className="logs-list">
      {logs.map((entry) => (
        <div key={`${entry.run_id}-${entry.seq}`} className="log-line">
          <span>{entry.seq}</span>
          <span>{entry.level}</span>
          <code>{entry.source}</code>
          <p>{entry.message}</p>
        </div>
      ))}
    </div>
  );
}
