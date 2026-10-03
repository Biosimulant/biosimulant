// @vitest-environment jsdom

import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { VisualRenderer } from "./visuals";
import type { RunVisualSpec } from "../types";

const molstarMocks = vi.hoisted(() => {
  const loadStructureFromUrl = vi.fn(async () => undefined);
  const dispose = vi.fn();
  const create = vi.fn(async () => ({ loadStructureFromUrl, dispose }));
  return { create, dispose, loadStructureFromUrl };
});

vi.mock("molstar/lib/apps/viewer/app", () => ({
  Viewer: { create: molstarMocks.create },
}));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let cleanup: (() => void) | null = null;

afterEach(() => {
  cleanup?.();
  cleanup = null;
  vi.clearAllMocks();
});

async function renderVisual(visual: RunVisualSpec, expanded = false) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);

  await act(async () => {
    root.render(<VisualRenderer visual={visual} expanded={expanded} />);
  });
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });

  cleanup = () => {
    act(() => root.unmount());
    container.remove();
  };

  return container;
}

it("dispatches every supported non-structure renderer", async () => {
  const cases: Array<[RunVisualSpec, (container: HTMLElement) => void]> = [
    [
      { render: "table", data: { rows: [{ Metric: "binder", Value: 0.91 }] } },
      (container) => expect(container.querySelector("table")?.textContent).toContain("binder"),
    ],
    [
      { render: "image", data: { src: "/artifacts/plot.png", alt: "plot" } },
      (container) => expect(container.querySelector("img.image-visual")?.getAttribute("src")).toBe("/artifacts/plot.png"),
    ],
    [
      { render: "timeseries", data: { series: [{ name: "signal", points: [[0, 1], [1, 2]] }] } },
      (container) => expect(container.querySelector("svg.chart polyline")).not.toBeNull(),
    ],
    [
      { render: "line", data: { series: [{ name: "signal", points: [[0, 1], [1, 2]] }] } },
      (container) => expect(container.querySelector("svg.chart polyline")).not.toBeNull(),
    ],
    [
      { render: "bar", data: { items: [{ label: "score", value: 4 }] } },
      (container) => expect(container.querySelector(".bar-list")?.textContent).toContain("score"),
    ],
    [
      { render: "scatter", data: { points: [{ x: 1, y: 2 }, { x: 2, y: 3 }] } },
      (container) => expect(container.querySelector("svg.chart circle")).not.toBeNull(),
    ],
    [
      { render: "heatmap", data: { matrix: [[0.2, 0.8], [0.4, 1]] } },
      (container) => expect(container.querySelector(".heatmap td")).not.toBeNull(),
    ],
    [
      { render: "graph", data: { nodes: [{ id: "a" }], edges: [{ source: "a", target: "b" }] } },
      (container) => expect(container.textContent).toContain("1 nodes / 1 edges"),
    ],
    [
      { render: "text", data: { text: "plain answer" } },
      (container) => expect(container.textContent).toContain("plain answer"),
    ],
    [
      { render: "json", data: { answer: 42 } },
      (container) => expect(container.textContent).toContain('"answer": 42'),
    ],
  ];

  for (const [visual, assertion] of cases) {
    const container = await renderVisual(visual);
    expect(container.textContent).not.toContain("Unsupported renderer");
    assertion(container);
    cleanup?.();
    cleanup = null;
  }
});

it("falls back with a clear unsupported renderer state", async () => {
  const container = await renderVisual({ render: "custom-widget", data: { value: 1 } });

  expect(container.textContent).toContain("Unsupported renderer: custom-widget");
  expect(container.textContent).toContain('"value": 1');
});

it("shows structure3d validation states before loading Molstar", async () => {
  const missingUrl = await renderVisual({ render: "structure3d", data: { format: "mmcif" } });
  expect(missingUrl.textContent).toContain("Structure artifact URL is missing.");
  cleanup?.();
  cleanup = null;

  const unsupportedFormat = await renderVisual({
    render: "structure3d",
    data: { format: "xyz", source: { url: "/api/runs/run/artifacts/structure" } },
  });
  expect(unsupportedFormat.textContent).toContain("Unsupported structure format.");
  expect(molstarMocks.create).not.toHaveBeenCalled();
});

it("loads structure3d artifacts through Molstar with the sanitized URL", async () => {
  await renderVisual(
    {
      render: "structure3d",
      data: {
        title: "Predicted Complex Structure",
        format: "cif",
        source: { url: "/api/runs/run-1/artifacts/complex" },
        annotations: [{ label: "Confidence", value: 0.91 }],
      },
    },
    true,
  );

  expect(molstarMocks.create).toHaveBeenCalled();
  expect(molstarMocks.loadStructureFromUrl).toHaveBeenCalledWith(
    "/api/runs/run-1/artifacts/complex",
    "mmcif",
    false,
    { label: "Predicted Complex Structure" },
  );
  expect(document.body.textContent).toContain("Confidence");
  expect(document.body.textContent).toContain("0.91");
});

it("shows structure3d loading and viewer failure states", async () => {
  molstarMocks.loadStructureFromUrl.mockReturnValueOnce(new Promise(() => undefined));
  const loading = await renderVisual({
    render: "structure3d",
    data: { format: "pdb", source: { url: "/api/runs/run/artifacts/pdb" } },
  });
  expect(loading.textContent).toContain("Loading structure...");
  cleanup?.();
  cleanup = null;

  molstarMocks.loadStructureFromUrl.mockRejectedValueOnce(new Error("parse failed"));
  const failed = await renderVisual({
    render: "structure3d",
    data: { format: "mmcif", source: { url: "/api/runs/run/artifacts/cif" } },
  });
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });

  expect(failed.textContent).toContain("Could not load structure: parse failed");
});

it("renders canonical heatmap values with labels, units and differing colours", async () => {
  const container = await renderVisual({render: "heatmap", data: {values: [[1, 2]], x_labels: ["A", "B"], y_labels: ["Control"], unit: "CFU"}});
  expect(container.textContent).toContain("CFU");
  expect(container.textContent).toContain("Control");
  const cells = container.querySelectorAll<HTMLTableCellElement>(".heatmap td");
  expect(cells.length).toBe(2);
  expect(cells[0].textContent).toBe("1");
  expect(cells[0].style.background).not.toBe(cells[1].style.background);
});
it("draws graph nodes and edges rather than only a count", async () => {
  const container = await renderVisual({render: "graph", data: {nodes: [{id: "A"}, {id: "B"}], edges: [{source: "A", target: "B"}]}});
  expect(container.querySelectorAll("svg circle").length).toBe(2);
  expect(container.querySelectorAll("svg line").length).toBe(1);
});
it("renders the documented axis units", async () => {
  const container = await renderVisual({render: "timeseries", data: {series: [{name: "Population", points: [[0, 1], [1, 2]]}], x_label: "Time", x_unit: "h", y_label: "Population", y_unit: "CFU"}});
  expect(container.textContent).toContain("Time (h)");
  expect(container.textContent).toContain("Population (CFU)");
});

it("renders negative bars around zero and preserves quantity units", async () => {
  const container = await renderVisual({render: "bar", data: {items: [{label: "Residual", value: -2, unit: "CFU"}]}});
  expect(container.textContent).toContain("-2 CFU");
  const bar = container.querySelector<HTMLElement>(".bar-row i")!;
  expect(bar.style.width).toBe("50%");
  expect(bar.style.left).toBe("0%");
});
