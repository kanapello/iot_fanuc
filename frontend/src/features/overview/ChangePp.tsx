import { t } from "../../shared/i18n";

/** Change in percentage points: growth green, fall red; without a previous period — a note, not a zero. */
export function ChangePp({ change, comparable = true }: { change: number | null; comparable?: boolean }) {
  if (change === null || !comparable) return <span className="text-xs text-slate-500">{t.overview.noCompare}</span>;
  const value = Math.round(change * 1000) / 10;
  const color = value > 0 ? "text-emerald-400" : value < 0 ? "text-red-400" : "text-slate-400";
  return (
    <span className={`tabular text-sm font-semibold ${color}`}>
      {value > 0 ? "▲ +" : value < 0 ? "▼ " : ""}
      {value.toFixed(1)} {t.overview.pp}
    </span>
  );
}

