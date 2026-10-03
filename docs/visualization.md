# Visualization delivery

Almost every Lab and scientific result model should present a meaningful visualization of
its computed results. Keep typed scientific outputs as well. Infrastructure-only components
may omit a visualization with an explicit reason. Do not generate decorative charts or
invent data to satisfy a count.

Before authoring in Studio Chat or MCP, read `get_biosimulant_authoring_contract` with
`topics: ["visualization"]`. It returns the versioned catalog, payload examples, limits,
asset lifecycle and acceptance rules. Both agent surfaces share this contract.

| Render | Choose it for | Data contract |
| --- | --- | --- |
| `timeseries` | Dynamics and trajectories | `series: [{name, points: [[x, y], ...]}]`; `x_label`, `x_unit`, `y_label`, `y_unit` |
| `bar` | Categories or endpoints | `items: [{label, value, unit?}]` |
| `scatter` | Observed/predicted values, residuals | `points: [{x, y, label?, series?}]`; axis labels and units |
| `heatmap` | Spatial fields, matrices, sweeps | Rectangular `values: [[number, ...], ...]`; `x_labels`, `y_labels`, `unit` |
| `graph` | Networks | `nodes: [{id}]`, `edges: [{source, target}]` referencing those IDs |
| `table` | Exact values and metadata | `columns: [string, ...]`, `rows: [[cell, ...], ...]`, matching widths |
| `image` | Scientific images and annotated overlays | `source: {kind: "artifact", path: "outputs/annotated.png"}` and meaningful `alt`; optional `caption` |
| `structure3d` | Molecular coordinates | `format: "pdb"` or `"mmcif"`, artifact `source` |
| `text` | Interpretation, caveats, abstentions | Nonempty `text` |

Each v1 spec includes `schema_version: "1"`, `render` and a JSON-compatible `data`
mapping. Add `title`, labels, units and `description` to explain the scientific result.
`biosimulant.visualization_catalog()` supplies executable examples for all nine renderers.
The catalog uses the common portable subset across local Labs Serve, Web and Desktop.
Some clients also accept richer legacy payloads; that does not make those payloads portable.
Custom renderer names require actual client registration and are outside the v1 contract.

## Requirements and implementation

The MRS records the user's question, expected view, source output, units, uncertainty,
caveats and usability acceptance. The MTS selects render types, the owning module,
typed connections, execution lifecycle, bounded previews, asset retention and verification.
A table is useful for exact data but should accompany a plot when a plot makes the result
clearer. Select a meaningful image overlay for image analysis, scatter for prediction
validation, heatmap for spatial/matrix data and 3D for structures.

Declare requirements at the top level of `lab.yaml`:

```yaml
visualization:
  schema_version: "1"
  required:
    - module: summary
      render: timeseries
      min_count: 1
```

Names are actual composed runtime aliases, including child prefixes. A Lab may explicitly
opt out using `visualization: {schema_version: "1", opt_out_reason: "Explain why no user-facing visual helps"}`.
Declare required views in the parent Lab even when produced by a child. Older Labs with no
contract remain runnable; new authoring must either declare deliverables or justify an opt-out.

Implement `visualize()` on the result component using actual computed state. It returns one
spec or a list. It must not run inference again or advance the science. A finite downstream
analysis module uses `ExecutionPolicy.ONCE_AFTER_RUN` with real typed wiring. Temporal
modules record bounded observations during `execute`; `visualize()` reads them afterward.

## Retained images and coordinates

Emit image/structure files inside the run's output directory and reference them through
`data.source = {"kind": "artifact", "path": "outputs/annotated.png"}`. Also emit the path
through a typed file output when it is a scientific deliverable. Managed execution collects
visual-only assets using the same file types, limits and confinement as other output files,
then replaces paths with durable artifact IDs. Clients resolve IDs through the authorized
run context when the run is reopened. Web previews use temporary browser object URLs,
revoked on teardown; persisted results contain stable IDs rather than those URLs.

Small inline images must use a complete `data:image/png;base64,...` URI. Raw base64 is not
an image source. Avoid temporary paths and expiring remote links as delivered sources.
The v1 contract bounds each spec to 2 MB, each Lab to 64 visuals and chart data to 10,000
points/cells per series/matrix. Web artifact image previews are limited to 10 MB; larger
files remain downloadable. Retention limits still apply to the full artifact.

## End-to-end acceptance

Run the actual composition and inspect results JSON: typed outputs plus grouped `visuals`.
The SDK adds a `visualization` delivery report; invalid v1 payloads and `visualize()` errors
produce bounded diagnostics while scientific outputs remain accessible. Legacy envelopes
remain compatible, with diagnostics where their payload is not portable.

Verify chosen types, nonempty data, units, plotted values against typed outputs, labels,
retained files, reopening and exports. Visual count alone is insufficient. Required invalid
or missing visuals fail trusted Studio qualification and exact-revision MCP publication.
Managed run Passports independently validate payloads and require artifact-backed visuals
to refer to completed artifacts. Existing Labs with no declared contract retain their legacy
qualification behavior. Delivery checks establish usable outputs, not biological accuracy.

Studio `create_visualization` creates an ad-hoc chat chart resource. It does not implement
reusable `BioModule.visualize()` behavior and does not satisfy Lab delivery requirements.
