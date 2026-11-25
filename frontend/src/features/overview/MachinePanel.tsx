import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { Link } from "react-router-dom";
import { api, query, type Interval, type Live, type Overview, type OverviewMachine, type ParetoRow } from "../../api/client";
import { t } from "../../shared/i18n";
import { Icon } from "../../shared/Shell";
import { formatDuration, percent, STATE_COLORS, stateColor, stateLabel } from "../../shared/states";
import { ChangePp } from "./ChangePp";

function Metric({ label, value, change }: { label: string; value: string; change: React.ReactNode }) {
  return (
    <div className="rounded-lg bg-slate-800/60 p-3">
      <div className="text-xs text-slate-400">{label}</div>
      <div className="tabular text-xl font-bold">{value}</div>
      <div className="mt-0.5 min-h-[1.25rem]">{change}</div>
    </div>
  );
}

/** Change of a count: growth is not always good (downtime, alarms), so ``goodWhenUp`` picks the colour. */
function CountChange({ value, previous, goodWhenUp, format }: { value: number | null; previous: number | null; goodWhenUp: boolean; format: (v: number) => string }) {
  if (value === null || previous === null) return <span className="text-xs text-slate-500">{t.overview.noCompare}</span>;
  const diff = value - previous;
  const good = diff === 0 ? null : diff > 0 === goodWhenUp;
  const color = good === null ? "text-slate-400" : good ? "text-emerald-400" : "text-red-400";
  return (
    <span className={`tabular text-sm font-semibold ${color}`}>
      {diff > 0 ? "▲ +" : diff < 0 ? "▼ −" : ""}
      {format(Math.abs(diff))}
    </span>
  );
}

/** Machine of the overview: its figures for the period with changes, a mini timeline, the main downtime reasons. */
export function MachinePanel({ machine, overview, live, onClose }: { machine: OverviewMachine; overview: Overview; live: Live | undefined; onClose: () => void }) {
  const from = overview.period.from;
  const to = new Date(Math.min(Date.parse(overview.period.to), Date.parse(overview.period.now))).toISOString();
  const params = query({ machine_id: machine.machine_id, from, to });
  const timeline = useQuery({
    queryKey: ["panel-timeline", params],
    queryFn: () => api<{ intervals: Interval[] }>(`/timeline${params}`),
  });
  const pareto = useQuery({ queryKey: ["panel-pareto", params], queryFn: () => api<ParetoRow[]>(`/reports/pareto${params}`) });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const span = Date.parse(to) - Date.parse(from) || 1;
  const reasons = (pareto.data ?? []).filter((r) => r.reason_id !== null).slice(0, 3);
  const { utilization, oee, parts, downtime_s, alarms } = machine;
  const open = machine.passport_code ? `/m/${machine.passport_code}` : null;

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <aside className="relative flex h-full w-full max-w-md flex-col overflow-y-auto border-l border-slate-800 bg-slate-900 p-4 shadow-2xl">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <h2 className="truncate text-xl font-semibold">{machine.name}</h2>
            <div className="text-sm text-slate-400">{machine.area_name}</div>
            {live && (
              <span className="mt-1 inline-block rounded px-2 py-0.5 text-xs font-semibold text-white" style={{ backgroundColor: stateColor(live.state) }}>
                {stateLabel(live.state)}
                {live.program ? ` · ${live.program}` : ""}
              </span>
            )}
          </div>
          <button className="rounded-lg p-2 text-2xl leading-none text-slate-400 hover:bg-slate-800" onClick={onClose} aria-label={t.overview.panel.close}>
            ×
          </button>
        </div>
        <p className="mt-1 text-xs text-slate-500">{t.overview.periods[overview.period.key]}</p>

        <div className="mt-3 grid grid-cols-2 gap-2">
          <Metric
            label={`${t.overview.panel.utilization}${utilization.usual !== null ? ` (${t.overview.panel.usual} ${percent(utilization.usual)})` : ""}`}
            value={percent(utilization.value, 1)}
            change={<ChangePp change={utilization.previous === null || utilization.value === null ? null : utilization.value - utilization.previous} />}
          />
          <Metric
            label={t.overview.panel.oee}
            value={percent(oee.value, 1)}
            change={<ChangePp change={oee.previous === null || oee.value === null ? null : oee.value - oee.previous} />}
          />
          <Metric
            label={t.overview.panel.parts}
            value={String(parts.value ?? 0)}
            change={<CountChange value={parts.value} previous={parts.previous} goodWhenUp format={(v) => String(v)} />}
          />
          <Metric
            label={t.overview.panel.downtime}
            value={formatDuration(downtime_s.value ?? 0)}
            change={<CountChange value={downtime_s.value} previous={downtime_s.previous} goodWhenUp={false} format={formatDuration} />}
          />
          <Metric
            label={t.overview.panel.alarms}
            value={String(alarms.value ?? 0)}
            change={<CountChange value={alarms.value} previous={alarms.previous} goodWhenUp={false} format={(v) => String(v)} />}
          />
        </div>

        <h3 className="mt-5 text-sm font-semibold text-slate-300">{t.overview.panel.timeline}</h3>
        <div className="relative mt-2 h-8 overflow-hidden rounded bg-slate-800">
          {(timeline.data?.intervals ?? []).map((i, n) => {
            const left = ((Date.parse(i.start_ts) - Date.parse(from)) / span) * 100;
            const width = ((Date.parse(i.end_ts) - Date.parse(i.start_ts)) / span) * 100;
            return (
              <div
                key={n}
                className="absolute inset-y-0"
                title={`${stateLabel(i.state)} · ${formatDuration((Date.parse(i.end_ts) - Date.parse(i.start_ts)) / 1000)}`}
                style={{ left: `${left}%`, width: `${Math.max(width, 0.15)}%`, backgroundColor: STATE_COLORS[i.state] }}
              />
            );
          })}
        </div>
        <div className="mt-1 flex justify-between text-xs text-slate-500">
          <span>{new Date(from).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short", timeZone: overview.period.timezone })}</span>
          <span>{new Date(to).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short", timeZone: overview.period.timezone })}</span>
        </div>

        <h3 className="mt-5 text-sm font-semibold text-slate-300">{t.overview.panel.reasons}</h3>
        {pareto.data && !reasons.length && <p className="mt-1 text-sm text-slate-400">{t.overview.panel.noReasons}</p>}
        <ul className="mt-2 space-y-1.5 text-sm">
          {reasons.map((r) => (
            <li key={r.reason_id} className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-2">
                <span className="inline-block h-3 w-3 rounded-sm" style={{ backgroundColor: r.color ?? "#64748b" }} />
                {r.name}
              </span>
              <span className="tabular text-slate-300">{formatDuration(r.duration_s)}</span>
            </li>
          ))}
        </ul>

        <div className="mt-auto pt-6">
          {open && (
            <Link to={open} className="flex items-center justify-center gap-2 rounded-lg bg-sky-600 px-4 py-3 font-semibold text-white hover:bg-sky-500">
              <Icon name="machines" />
              {t.overview.panel.open}
            </Link>
          )}
        </div>
      </aside>
    </div>
  );
}
