// @vitest-environment jsdom

import * as React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it } from "vitest";
import { AgentPanel, connectCommand } from "./agent-panel";
import type { AgentConnection } from "../types";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let cleanup: (() => void) | null = null;

afterEach(() => {
  cleanup?.();
  cleanup = null;
});

const connection: AgentConnection = {
  enabled: true,
  read_only: false,
  token: "tok_123",
  path: "/mcp",
  tools: ["lab_get", "run_start"],
};

function render(value: AgentConnection | null) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => root.render(<AgentPanel connection={value} />));
  cleanup = () => {
    act(() => root.unmount());
    container.remove();
  };
  return container;
}

it("builds a connect command for this serve instance", () => {
  expect(connectCommand(connection, "http://127.0.0.1:8792/")).toBe(
    'claude mcp add --transport http biosimulant-lab http://127.0.0.1:8792/mcp --header "Authorization: Bearer tok_123"',
  );
});

it("leaves the header out when the endpoint needs no token", () => {
  expect(connectCommand({ ...connection, token: null }, "http://127.0.0.1:8792")).toBe(
    "claude mcp add --transport http biosimulant-lab http://127.0.0.1:8792/mcp",
  );
});

it("shows the command and what the agent can do", () => {
  const container = render(connection);

  expect(container.textContent).toContain("claude mcp add --transport http");
  expect(container.textContent).toContain("lab_get");
  expect(container.textContent).toContain("run_start");
});

it("says so when the agent may only read", () => {
  const container = render({ ...connection, read_only: true });

  expect(container.textContent).toContain("cannot change or run it");
});

it("explains how to open the endpoint when serve closed it", () => {
  const container = render({ ...connection, enabled: false });

  expect(container.textContent).toContain("--no-agent");
  expect(container.querySelector(".agent-command")).toBeNull();
});
