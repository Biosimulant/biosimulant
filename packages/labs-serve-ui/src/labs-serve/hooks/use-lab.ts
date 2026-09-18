import * as React from "react";
import { serveApi } from "../api";
import type { LocalLab } from "../types";

export type LabState = {
  lab: LocalLab | null;
  error: string | null;
  refreshing: boolean;
  refresh: () => Promise<void>;
  setLab: (lab: LocalLab) => void;
};

export function useLab(): LabState {
  const [lab, setLab] = React.useState<LocalLab | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [refreshing, setRefreshing] = React.useState(false);

  const refresh = React.useCallback(async () => {
    setRefreshing(true);
    try {
      const { lab: next } = await serveApi.lab();
      setLab(next);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRefreshing(false);
    }
  }, []);

  React.useEffect(() => {
    void refresh();
  }, [refresh]);

  React.useEffect(() => {
    const status = lab?.runtime_metadata_status;
    if (status !== "pending" && status !== "running") return;
    const timer = window.setTimeout(() => {
      void refresh();
    }, 1500);
    return () => window.clearTimeout(timer);
  }, [lab?.runtime_metadata_status, refresh]);

  // An agent edits the same lab through MCP, so the page keeps reading it back.
  React.useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      void refresh();
    }, 4000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  return { lab, error, refreshing, refresh, setLab };
}
