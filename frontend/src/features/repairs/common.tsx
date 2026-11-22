import { t } from "../../shared/i18n";

/** "40 с", "1 мин 10 с", "45 мин", "3 ч 20 мин", "2 дн 5 ч". */
export function formatSpan(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const whole = Math.max(0, Math.round(seconds));
  if (whole < 60) return `${whole} ${t.units.s}`;
  if (whole < 600) return `${Math.floor(whole / 60)} ${t.units.min}${whole % 60 ? ` ${whole % 60} ${t.units.s}` : ""}`;
  const minutes = Math.round(whole / 60);
  if (minutes < 60) return `${minutes} ${t.units.min}`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} ${t.units.h} ${minutes % 60} ${t.units.min}`;
  return `${Math.floor(hours / 24)} ${t.units.d} ${hours % 24} ${t.units.h}`;
}

export const formatMoment = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";

export const formatDay = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleDateString("ru-RU") : "—");

const STATUS_STYLE: Record<string, string> = {
  new: "bg-amber-500 text-slate-950",
  accepted: "bg-sky-600 text-white",
  in_progress: "bg-blue-600 text-white",
  done: "bg-green-600 text-white",
  voided: "bg-slate-600 text-slate-200",
};

export function RequestStatusChip({ status, voided }: { status: string; voided: boolean }) {
  const key = voided ? "voided" : status;
  return (
    <span className={`whitespace-nowrap rounded px-2 py-0.5 text-xs font-semibold ${STATUS_STYLE[key]}`}>
      {voided ? t.repairs.voided : t.repairs.statuses[status]}
    </span>
  );
}

// maintenance status: ok — the RUN colour, soon — IDLE, due — STOP (one palette for every screen)
const PLAN_STYLE: Record<string, string> = {
  ok: "bg-green-600 text-white",
  soon: "bg-yellow-500 text-slate-950",
  due: "bg-red-600 text-white",
};

export function PlanStatusChip({ status }: { status: string }) {
  return <span className={`whitespace-nowrap rounded px-2 py-0.5 text-xs font-semibold ${PLAN_STYLE[status]}`}>{t.maintenance.status[status]}</span>;
}

export const machinePlace = (row: { org_name: string; site_name: string; area_name: string | null }) =>
  [row.org_name, row.site_name, row.area_name].filter(Boolean).join(" · ");

/** Local "now" for <input type="datetime-local">. */
export function localInputNow(): string {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

export const button = "rounded bg-slate-700 px-3 py-1 text-sm hover:bg-slate-600";
export const primary = "rounded bg-sky-600 px-3 py-1 text-sm font-medium hover:bg-sky-500";
export const input = "rounded border border-slate-600 bg-slate-800 px-2 py-1 text-sm";
