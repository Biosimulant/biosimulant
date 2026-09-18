import {
  ArrowsCounterClockwiseIcon,
  CircleNotchIcon,
  MoonIcon,
  PlayIcon,
  SidebarSimpleIcon,
  StopCircleIcon,
  SunIcon,
} from "@phosphor-icons/react";
import type { AgentConnection, LocalLab, LocalRun } from "../types";
import { isActive } from "../hooks/use-runs";
import type { ThemeMode } from "../hooks/use-theme";
import { AgentStatus } from "./agent-panel";

export type HeaderProps = {
  lab: LocalLab | null;
  activeRun: LocalRun | null;
  busy: boolean;
  agent: AgentConnection | null;
  onToggleLeft: () => void;
  onToggleRight: () => void;
  onRefresh: () => void;
  onRunClick: () => void;
  onCancel: () => void;
  onAgentClick: () => void;
  theme: ThemeMode;
  onThemeChange: (next: ThemeMode) => void;
};

function nextTheme(current: ThemeMode): ThemeMode {
  if (current === "system") return "light";
  if (current === "light") return "dark";
  return "system";
}

/** Long lab paths read better from the end, where the lab's own folder is. */
function shortPath(path: string | undefined): string {
  if (!path) return "";
  const parts = path.split("/").filter(Boolean);
  return parts.length <= 2 ? path : `…/${parts.slice(-2).join("/")}`;
}

export function Header(props: HeaderProps) {
  const {
    lab,
    activeRun,
    busy,
    agent,
    onToggleLeft,
    onToggleRight,
    onRefresh,
    onRunClick,
    onCancel,
    onAgentClick,
    theme,
    onThemeChange,
  } = props;

  const running = isActive(activeRun);
  const cancelling = activeRun?.status === "cancelling";
  const path = lab?.file_path || lab?.id || "";

  return (
    <header className="command-bar">
      <button
        className="icon-button"
        title="Toggle lab contents"
        onClick={onToggleLeft}
        aria-label="Toggle lab contents"
      >
        <SidebarSimpleIcon size={16} />
      </button>
      <div className="command-title">
        <h1>{lab?.title || "Biosimulant Lab"}</h1>
        <p title={path}>{shortPath(path) || "Loading lab…"}</p>
      </div>
      <div className="command-actions">
        <button className="button ghost" onClick={onAgentClick} title="Connect an agent to this lab">
          <AgentStatus connection={agent} />
          {agent?.enabled ? null : "Agent"}
        </button>
        {running ? (
          <button className="button danger" disabled={busy || cancelling} onClick={onCancel}>
            {cancelling ? <CircleNotchIcon size={14} className="spin" /> : <StopCircleIcon size={14} />}
            {cancelling ? "Cancelling" : "Cancel"}
          </button>
        ) : (
          <button className="button primary" disabled={busy} onClick={onRunClick}>
            {busy ? <CircleNotchIcon size={14} className="spin" /> : <PlayIcon size={14} />}
            Run
          </button>
        )}
        <button className="icon-button" title="Refresh" onClick={onRefresh} aria-label="Refresh">
          <ArrowsCounterClockwiseIcon size={15} />
        </button>
        <button
          className="icon-button"
          title={`Theme: ${theme}`}
          aria-label={`Theme: ${theme}`}
          onClick={() => onThemeChange(nextTheme(theme))}
        >
          {theme === "dark" ? <MoonIcon size={15} /> : <SunIcon size={15} />}
        </button>
        <button
          className="icon-button"
          title="Toggle results panel"
          onClick={onToggleRight}
          aria-label="Toggle results panel"
        >
          <SidebarSimpleIcon size={16} />
        </button>
      </div>
    </header>
  );
}
