import { useQuery } from "@tanstack/react-query";
import type { EChartsOption } from "echarts";
import { useMemo, useState } from "react";
import { api, query, type CycleStats, type CycleSummary } from "../../api/client";
import { Chart } from "../../shared/Chart";
import { t } from "../../shared/i18n";
import { formatDuration, percent } from "../../shared/states";

const PERIOD_DAYS: Record<string, number> = { day: 1, week: 7, month: 30 };
const seconds = (value: number | null | undefined) => (value == null ? "—" : formatDuration(value));
const rowKey = (row: CycleStats) => `${row.machine_id}|${row.program ?? ""}`;

function histogramOption(bins: CycleStats["machine_hist"], color: string): EChartsOption {
  return {
    grid: { left: 40, right: 10, top: 10, bottom: 40 },
    tooltip: { trigger: "axis" },
    xAxis: { type: "category", data: bins.map((b) => `${formatDuration(b.from)}–${formatDuration(b.to)}`), axisLabel: { rotate: 30 } },
    yAxis: { type: "value", minInterval: 1 },
    series: [{ type: "bar", data: bins.map((b) => b.count), itemStyle: { color } }],
  };
}

function SummaryTable({ title, rows }: { title: string; rows: (CycleSummary & { label: string })[] }) {
  return (
    <div>
      <h4 className="mb-1 text-sm font-semibold text-slate-300">{title}</h4>
      <table className="w-full text-sm">
        <thead className="text-left text-slate-400">
          <tr>
            <th className="px-2 py-1 font-medium" />
            <th className="px-2 py-1 text-right font-medium">{t.cycles.count}</th>
            <th className="px-2 py-1 text-right font-medium">{t.cycles.machining}</th>
            <th className="px-2 py-1 text-right font-medium">{t.cycles.load}</th>
            <th className="px-2 py-1 text-right font-medium">{t.cycles.interruptions}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.label} className="tabular border-t border-slate-800">
              <td className="px-2 py-1">{r.label}</td>
              <td className="px-2 py-1 text-right">{r.cycles}</td>
              <td className="px-2 py-1 text-right">{seconds(r.machine_median_s)}</td>
              <td className="px-2 py-1 text-right">{seconds(r.load_median_s)}</td>
              <td className="px-2 py-1 text-right">{percent(r.interrupt_share, 1)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Detail({ row }: { row: CycleStats }) {
  const machineOption = useMemo(() => histogramOption(row.machine_hist, "#22c55e"), [row]);
  const loadOption = useMemo(() => histogramOption(row.load_hist, "#eab308"), [row]);
  const shiftLabel = (s: CycleStats["by_shift"][number]) =>
    `${s.name}, ${new Date(s.start).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}`;
  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-400">
        {t.cycles.threshold}: {formatDuration(row.threshold_s)} ({row.threshold_auto ? t.cycles.auto : t.cycles.machineSetting}) ·{" "}
        {t.cycles.excluded}: {t.cycles.setup} {row.setup}, {t.cycles.incomplete} {row.incomplete}
      </p>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div>
          <h4 className="text-sm font-semibold text-slate-300">{t.cycles.machiningHist}</h4>
          <Chart option={machineOption} height={200} />
        </div>
        <div>
          <h4 className="text-sm font-semibold text-slate-300">{t.cycles.loadHist}</h4>
          {row.load_hist.length ? <Chart option={loadOption} height={200} /> : <p className="text-slate-400">{t.common.noData}</p>}
        </div>
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <SummaryTable title={t.cycles.byShift} rows={row.by_shift.map((s) => ({ ...s, label: shiftLabel(s) }))} />
        <SummaryTable title={t.cycles.byDay} rows={row.by_day.map((d) => ({ ...d, label: new Date(d.day).toLocaleDateString("ru-RU") }))} />
      </div>
    </div>
  );
}

/** Cycles by program: machine time, part change, interruptions and the norm (worker builds them with the timeline). */
export function CyclesCard({ siteId, areaId }: { siteId: number | null; areaId: number | null }) {
  const [period, setPeriod] = useState("week");
  const [open, setOpen] = useState<string | null>(null);
  const from = useMemo(() => new Date(Date.now() - PERIOD_DAYS[period] * 86_400_000).toISOString(), [period]);
  const params = query({ from, site_id: siteId, area_id: areaId });
  const { data: rows, isLoading } = useQuery({
    queryKey: ["cycles", params],
    queryFn: () => api<CycleStats[]>(`/cycles/stats${params}`),
    enabled: siteId !== null,
    refetchInterval: 60_000,
  });
  const tab = (active: boolean) => `rounded px-3 py-1 text-sm ${active ? "bg-slate-700 text-white" : "text-slate-300 hover:bg-slate-800"}`;
  return (
    <section className="space-y-3 rounded-xl bg-slate-900 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="mr-2 text-lg font-semibold">{t.cycles.title}</h2>
        {Object.keys(PERIOD_DAYS).map((p) => (
          <button key={p} className={tab(period === p)} onClick={() => setPeriod(p)}>
            {t.alarms.periods[p]}
          </button>
        ))}
      </div>
      <p className="text-xs text-slate-400">{t.cycles.hint}</p>
      {isLoading && <p className="text-slate-400">{t.common.loading}</p>}
      {rows?.length === 0 && <p className="text-slate-400">{t.common.noData}</p>}
      {Boolean(rows?.length) && (
        <table className="w-full text-sm">
          <thead className="text-left text-slate-400">
            <tr>
              <th className="px-2 py-1 font-medium">{t.analytics.machine}</th>
              <th className="px-2 py-1 font-medium">{t.cycles.program}</th>
              <th className="px-2 py-1 text-right font-medium">{t.cycles.count}</th>
              <th className="px-2 py-1 text-right font-medium">{t.cycles.machining}</th>
              <th className="px-2 py-1 text-right font-medium">{t.cycles.load}</th>
              <th className="px-2 py-1 text-right font-medium">{t.cycles.cycle}</th>
              <th className="px-2 py-1 text-right font-medium">{t.cycles.interruptions}</th>
              <th className="px-2 py-1 text-right font-medium">{t.cycles.pauses}</th>
              <th className="px-2 py-1 text-right font-medium">{t.cycles.normPerPart}</th>
              <th className="px-2 py-1 text-right font-medium">{t.cycles.vsNorm}</th>
            </tr>
          </thead>
          <tbody>
            {(rows ?? []).map((row) => {
              const key = rowKey(row);
              return (
                <CycleRow key={key} row={row} open={open === key} onToggle={() => setOpen(open === key ? null : key)} />
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

function CycleRow({ row, open, onToggle }: { row: CycleStats; open: boolean; onToggle: () => void }) {
  const slow = row.vs_norm != null && row.vs_norm > 1.05;
  return (
    <>
      <tr className="tabular cursor-pointer border-t border-slate-800 hover:bg-slate-800" onClick={onToggle}>
        <td className="px-2 py-1">{row.machine_name}</td>
        <td className="px-2 py-1 font-medium">{row.program ?? "—"}</td>
        <td className="px-2 py-1 text-right">
          {row.cycles} / {row.total}
        </td>
        <td className="px-2 py-1 text-right">{seconds(row.machine_median_s)}</td>
        <td className="px-2 py-1 text-right">{seconds(row.load_median_s)}</td>
        <td className="px-2 py-1 text-right">{seconds(row.cycle_median_s)}</td>
        <td className="px-2 py-1 text-right">{percent(row.interrupt_share, 1)}</td>
        <td className="px-2 py-1 text-right">{row.pauses ? `${row.pauses} · ${formatDuration(row.pause_s)}` : "—"}</td>
        <td className="px-2 py-1 text-right">
          {seconds(row.norm_per_part_s)} / {seconds(row.machine_per_part_median_s)}
        </td>
        <td className={`px-2 py-1 text-right font-semibold ${slow ? "text-amber-300" : ""}`}>{percent(row.vs_norm, 0)}</td>
      </tr>
      {open && (
        <tr className="bg-slate-950/40">
          <td colSpan={10} className="px-3 py-3">
            <Detail row={row} />
          </td>
        </tr>
      )}
    </>
  );
}
