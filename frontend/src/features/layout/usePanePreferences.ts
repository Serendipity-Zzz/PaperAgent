import { useEffect, useState } from "react";

function readNumber(key: string, fallback: number, minimum: number, maximum: number): number {
  const stored = window.localStorage.getItem(key);
  if (stored === null) return fallback;
  const parsed = Number(stored);
  return Number.isFinite(parsed) ? Math.min(maximum, Math.max(minimum, parsed)) : fallback;
}

function readBoolean(key: string, fallback: boolean): boolean {
  const stored = window.localStorage.getItem(key);
  return stored === null ? fallback : stored === "true";
}

export function usePanePreferences() {
  const [sidebarWidth, setSidebarWidth] = useState(() => readNumber("paperagent.sidebar.width", 260, 190, 420));
  const [previewWidth, setPreviewWidth] = useState(() => readNumber("paperagent.preview.width", 42, 28, 65));
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => readBoolean("paperagent.sidebar.collapsed", false));

  useEffect(() => window.localStorage.setItem("paperagent.sidebar.width", String(sidebarWidth)), [sidebarWidth]);
  useEffect(() => window.localStorage.setItem("paperagent.preview.width", String(previewWidth)), [previewWidth]);
  useEffect(() => window.localStorage.setItem("paperagent.sidebar.collapsed", String(sidebarCollapsed)), [sidebarCollapsed]);

  return {
    sidebarWidth,
    setSidebarWidth,
    previewWidth,
    setPreviewWidth,
    sidebarCollapsed,
    setSidebarCollapsed,
  };
}
