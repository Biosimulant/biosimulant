// @vitest-environment jsdom

import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import type { LocalLab } from "../types";
import { Canvas } from "./canvas";

vi.mock("@xyflow/react", async () => {
  const React = await import("react");
  return {
    MarkerType: { ArrowClosed: "arrowclosed" },
    Position: { Left: "left", Right: "right" },
    Background: () => null,
    Controls: () => null,
    Handle: () => null,
    Panel: ({ children }: { children?: React.ReactNode }) =>
      React.createElement("div", { className: "mock-flow-panel" }, children),
    ReactFlowProvider: ({ children }: { children?: React.ReactNode }) =>
      React.createElement("div", { className: "mock-flow-provider" }, children),
    ReactFlow: ({
      children,
      nodes,
    }: {
      children?: React.ReactNode;
      nodes: Array<{ id: string; data?: { title?: string } }>;
    }) =>
      React.createElement(
        "div",
        { "data-testid": "react-flow" },
        nodes.map((node) =>
          React.createElement(
            "div",
            { key: node.id, "data-node-id": node.id },
            node.data?.title ?? node.id,
          ),
        ),
        children,
      ),
    useEdgesState: (initial: unknown[]) => {
      const [edges, setEdges] = React.useState(initial);
      return [edges, setEdges, () => undefined];
    },
    useNodesState: (initial: unknown[]) => {
      const [nodes, setNodes] = React.useState(initial);
      return [nodes, setNodes, () => undefined];
    },
    useReactFlow: () => ({ fitView: () => undefined }),
    // The canvas reads the live zoom to decide how much node detail to draw.
    useStore: (selector: (state: { transform: [number, number, number] }) => unknown) =>
      selector({ transform: [0, 0, 1] }),
  };
});

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let cleanup: (() => void) | null = null;

afterEach(() => {
  cleanup?.();
  cleanup = null;
});

function renderCanvas(lab: LocalLab | null, loading = false) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);

  act(() => {
    root.render(
      <Canvas
        lab={lab}
        loading={loading}
        selection={{ kind: "world" }}
        onSelect={() => undefined}
      />,
    );
  });

  cleanup = () => {
    act(() => root.unmount());
    container.remove();
  };

  return container;
}

function localLab(overrides: Partial<LocalLab> = {}): LocalLab {
  return {
    id: "lab",
    title: "Lab",
    description: null,
    tags: [],
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    manifest: {
      models: [],
      children: [],
      wiring: [],
      io: { inputs: [], outputs: [] },
    },
    ...overrides,
  };
}

it("shows a loading state before the first lab payload arrives", () => {
  const container = renderCanvas(null, true);

  expect(container.textContent).toContain("Loading lab...");
  expect(container.textContent).not.toContain("No modules in this lab");
});

it("shows the empty-lab state only after a loaded lab has no modules", () => {
  const container = renderCanvas(localLab());

  expect(container.textContent).toContain("No modules in this lab");
  expect(container.textContent).not.toContain("Loading lab...");
});

it("renders manifest graph data while runtime metadata is still preparing", () => {
  const container = renderCanvas(
    localLab({
      runtime_metadata_status: "running",
      manifest: {
        models: [
          {
            alias: "counter",
            path: "owned/models/counter",
            resolved_model: {
              title: "Counter",
              io: { inputs: [], outputs: [{ name: "count" }] },
            },
          },
        ],
        children: [],
        wiring: [],
        io: { inputs: [], outputs: [] },
      },
    }),
  );

  expect(container.textContent).toContain("Counter");
  expect(container.textContent).toContain("Preparing runtime metadata...");
  expect(container.textContent).not.toContain("No modules in this lab");
});
