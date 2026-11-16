import { t } from "./i18n";

// Single palette for every screen (CLAUDE.md).
export const STATE_COLORS: Record<string, string> = {
  RUN: "#22c55e",
  IDLE: "#eab308",
  STOP: "#ef4444",
  ALARM: "#a855f7",
  SETUP: "#3b82f6",
  OFFLINE: "#6b7280",
};
export const STATE_ORDER = ["RUN", "IDLE", "STOP", "ALARM", "SETUP", "OFFLINE"];

export const stateColor = (state?: string | null) => STATE_COLORS[state ?? "OFFLINE"] ?? STATE_COLORS.OFFLINE;
export const stateLabel = (state?: string | null) => t.state[state ?? "OFFLINE"] ?? state ?? "";

export function formatDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s % 60)}` : `${pad(m)}:${pad(s % 60)}`;
}

export const percent = (value?: number | null, digits = 0) =>
  value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)}%`;

export const formatTime = (iso: string | number | Date) =>
  new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
