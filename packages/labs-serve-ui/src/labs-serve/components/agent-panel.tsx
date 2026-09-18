import * as React from "react";
import { CheckIcon, CopyIcon, PlugsConnectedIcon } from "@phosphor-icons/react";
import type { AgentConnection } from "../types";

export function connectCommand(connection: AgentConnection, origin: string): string {
  const url = `${origin.replace(/\/$/, "")}${connection.path}`;
  const header = connection.token ? ` --header "Authorization: Bearer ${connection.token}"` : "";
  return `claude mcp add --transport http biosimulant-lab ${url}${header}`;
}

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = React.useState(false);

  React.useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);

  return (
    <button
      type="button"
      className="button ghost small"
      onClick={() => {
        void navigator.clipboard?.writeText(value).then(() => setCopied(true));
      }}
    >
      {copied ? <CheckIcon size={13} /> : <CopyIcon size={13} />}
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

export function AgentPanel({ connection }: { connection: AgentConnection | null }) {
  const origin = typeof window === "undefined" ? "" : window.location.origin;

  if (!connection) {
    return <p className="muted">Checking whether this lab is open to an agent…</p>;
  }

  if (!connection.enabled) {
    return (
      <div className="agent-panel">
        <p className="muted">
          This lab is served without an agent endpoint. Restart without{" "}
          <code>--no-agent</code> to let an agent read and change it.
        </p>
      </div>
    );
  }

  const command = connectCommand(connection, origin);

  return (
    <div className="agent-panel">
      <p className="agent-lead">
        Your agent does the building. Point it at this lab and it reads, edits and runs the
        same copy this page is showing.
      </p>

      <div className="agent-command">
        <pre>
          <code>{command}</code>
        </pre>
        <CopyButton value={command} />
      </div>

      <p className="muted small">
        The token lasts as long as this serve command. Codex and Claude Desktop take the same
        URL and header in their own MCP settings.
      </p>

      {connection.read_only ? (
        <p className="agent-note">
          Read-only: the agent can read this lab but cannot change or run it.
        </p>
      ) : null}

      <div className="agent-tools">
        <h3>What it can do</h3>
        <ul>
          {connection.tools.map((tool) => (
            <li key={tool}>
              <code>{tool}</code>
            </li>
          ))}
        </ul>
      </div>

      <p className="muted small">
        For a managed run on Biosimulant rather than this machine, use{" "}
        <code>biosimulant runs create</code>.
      </p>
    </div>
  );
}

export function AgentStatus({ connection }: { connection: AgentConnection | null }) {
  if (!connection?.enabled) return null;
  return (
    <span className="agent-status" title="This lab is open to a local agent over MCP">
      <PlugsConnectedIcon size={13} />
      Agent ready
    </span>
  );
}
