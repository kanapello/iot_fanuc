import { useQuery } from "@tanstack/react-query";
import type { EChartsOption } from "echarts";
import { useMemo, useState } from "react";
import { api, query, type Interval, type Machine, type OeeRow, type ParetoRow, type Shift } from "../../api/client";
import { Chart } from "../../shared/Chart";
import { t } from "../../shared/i18n";
import { NodePicker, useMe, useNodeSelection } from "../../shared/session";
import { formatDuration, formatTime, percent, STATE_COLORS, STATE_ORDER, stateColor, stateLabel } from "../../shared/states";

type Preset = "currentShift" | "previousShift" | "today" | "yesterday";
const PRESETS: Preset[] = ["currentShift", "previousShift", "today", "yesterday"];

function resolveRange(preset: Preset, shifts: Shift[] | undefined): { from: string; to: string } | null {
  const now = new Date();
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  if (preset === "today") return { from: midnight.toISOString(), to: now.toISOString() };
  if (preset === "yesterday") {
    return { from: new Date(midnight.getTime() - 86400000).toISOString(), to: midnight.toISOString() };
  }
  if (!shifts) return null;
  const started = shifts.filter((s) => Date.parse(s.start) <= now.getTime());
  const shift = started[started.length - (preset === "currentShift" ? 1 : 2)];
  if (!shift) return null;
  return { from: shift.start, to: new Date(Math.min(Date.parse(shift.end), now.getTime())).toISOString() };
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-xl bg-slate-900 p-4">
      <h2 className="mb-2 text-lg font-semibold">{title}</h2>
      {children}
    </section>
  );
}

export function AnalyticsPage() {
  const [preset, setPreset] = useState<Preset>("currentShift");
  const { data: me } = useMe();
  // shift boundaries are a property of a site, so analytics always works on one site
  const selection = useNodeSelection(me?.sites, { requireSite: true });
  const { siteId, areaId } = selection;
  const anchor = useMemo(() => new Date(Date.now() - 48 * 3600 * 1000).toISOString(), []);

  const { data: machines } = useQuery({ queryKey: ["machines"], queryFn: () => api<Machine[]>("/machines") });
  const { data: shifts } = useQuery({
    queryKey: ["shifts", siteId],
    queryFn: () => api<Shift[]>(`/shifts${query({ from: anchor, site_id: siteId })}`),
    enabled: siteId !== null,
  });

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const range = useMemo(() => resolveRange(preset, shifts), [preset, shifts]);
  const params = range ? query({ from: range.from, to: range.to, site_id: siteId, area_id: areaId }) : "";
  const enabled = Boolean(range);
  const refetchInterval = 30000;

  const oee = useQuery({
    queryKey: ["oee", params],
    queryFn: () => api<OeeRow[]>(`/oee${params}&period=total`),
    enabled,
    refetchInterval,
  });
  const timeline = useQuery({
    queryKey: ["timeline", params],
    queryFn: () => api<{ intervals: Interval[] }>(`/timeline${params}`),
    enabled,
    refetchInterval,
  });
  const pareto = useQuery({
    queryKey: ["pareto", params],
    queryFn: () => api<ParetoRow[]>(`/reports/pareto${params}`),
    enabled,
    refetchInterval,
  });

  const shown = useMemo(
    () => (machines ?? []).filter((m) => m.site_id === siteId && (!areaId || m.area_id === areaId)),
    [machines, siteId, areaId],
  );
  const nameOf = useMemo(() => Object.fromEntries(shown.map((m) => [m.id, m.name])), [shown]);

  const oeeOption = useMemo<EChartsOption>(() => {
    const rows = shown.map((m) => oee.data?.find((r) => r.machine_id === m.id));
    const series = (name: string, key: "availability" | "performance" | "quality" | "oee", color: string) => ({
      name,
      type: "bar" as const,
      itemStyle: { color },
      data: rows.map((r) => (r?.[key] == null ? null : Math.round(r[key]! * 1000) / 10)),
    });
    return {
      tooltip: { trigger: "axis", valueFormatter: (v) => (v == null ? "—" : `${v}%`) },
      legend: { top: 0 },
      grid: { left: 45, right: 15, top: 40, bottom: 30 },
      xAxis: { type: "category", data: shown.map((m) => m.name) },
      yAxis: { type: "value", axisLabel: { formatter: "{value}%" } },
      series: [
        series(t.analytics.availability, "availability", "#38bdf8"),
        series(t.analytics.performance, "performance", "#a78bfa"),
        series(t.analytics.quality, "quality", "#34d399"),
        series(t.analytics.oee, "oee", "#f8fafc"),
      ],
    };
  }, [oee.data, shown]);

  const ganttOption = useMemo<EChartsOption>(() => {
    const ids = shown.map((m) => m.id);
    const data = (timeline.data?.intervals ?? [])
      .filter((i) => ids.includes(i.machine_id))
      .map((i) => ({
        value: [ids.indexOf(i.machine_id), Date.parse(i.start_ts), Date.parse(i.end_ts), i.state, i.program ?? ""],
        itemStyle: { color: stateColor(i.state) },
      }));
    return {
      tooltip: {
        formatter: (p: any) => {
          const [row, start, end, state, program] = p.value;
          return `<b>${nameOf[ids[row]] ?? ""}</b><br/>${stateLabel(state)} · ${program}<br/>${formatTime(start)} – ${formatTime(end)} (${formatDuration((end - start) / 1000)})`;
        },
      },
      grid: { left: 110, right: 15, top: 10, bottom: 60 },
      dataZoom: [{ type: "slider", filterMode: "weakFilter", height: 18, bottom: 8 }, { type: "inside", filterMode: "weakFilter" }],
      xAxis: {
        type: "time",
        min: range ? Date.parse(range.from) : undefined,
        max: range ? Date.parse(range.to) : undefined,
      },
      yAxis: { type: "category", data: shown.map((m) => m.name), inverse: true },
      series: [
        {
          type: "custom",
          clip: true,
          encode: { x: [1, 2], y: 0 },
          data,
          renderItem: (_params: any, apiRef: any) => {
            const row = apiRef.value(0);
            const start = apiRef.coord([apiRef.value(1), row]);
            const end = apiRef.coord([apiRef.value(2), row]);
            const height = apiRef.size([0, 1])[1] * 0.62;
            return {
              type: "rect",
              shape: { x: start[0], y: start[1] - height / 2, width: Math.max(end[0] - start[0], 1), height },
              style: apiRef.style(),
            };
          },
        } as any,
      ],
    };
  }, [timeline.data, shown, nameOf, range]);

  const paretoRows = useMemo(
    () =>
      (pareto.data ?? [])
        .filter((r) => r.state !== "IDLE")
        .map((r) => ({
          label: r.name ?? `${stateLabel(r.state)} (${t.analytics.withoutReason})`,
          color: r.color ?? STATE_COLORS[r.state ?? "STOP"] ?? "#64748b",
          minutes: r.duration_s / 60,
          count: r.count,
        })),
    [pareto.data],
  );
  const paretoOption = useMemo<EChartsOption>(() => {
    const total = paretoRows.reduce((sum, r) => sum + r.minutes, 0) || 1;
    let running = 0;
    const cumulative = paretoRows.map((r) => Math.round(((running += r.minutes) / total) * 1000) / 10);
    return {
      tooltip: { trigger: "axis" },
      grid: { left: 50, right: 50, top: 20, bottom: 90 },
      xAxis: { type: "category", data: paretoRows.map((r) => r.label), axisLabel: { rotate: 30, interval: 0 } },
      yAxis: [
        { type: "value", name: t.analytics.minutes },
        { type: "value", max: 100, axisLabel: { formatter: "{value}%" }, splitLine: { show: false } },
      ],
      series: [
        {
          name: t.analytics.minutes,
          type: "bar",
          data: paretoRows.map((r) => ({ value: Math.round(r.minutes * 10) / 10, itemStyle: { color: r.color } })),
        },
        { name: t.analytics.cumulative, type: "line", yAxisIndex: 1, data: cumulative, itemStyle: { color: "#f8fafc" } },
      ],
    };
  }, [paretoRows]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {PRESETS.map((p) => (
          <button
            key={p}
            onClick={() => setPreset(p)}
            className={`rounded px-3 py-2 text-sm font-medium ${preset === p ? "bg-sky-600" : "bg-slate-800 hover:bg-slate-700"}`}
          >
            {t.analytics[p]}
          </button>
        ))}
        {me && <NodePicker sites={me.sites} selection={selection} allowAllSites={false} />}
        {range && (
          <span className="text-sm text-slate-400">
            {new Date(range.from).toLocaleString("ru-RU")} – {new Date(range.to).toLocaleString("ru-RU")}
          </span>
        )}
      </div>

      {!range && <p className="text-slate-400">{shifts ? t.common.noData : t.common.loading}</p>}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card title={t.analytics.oeeByMachine}>
          <Chart option={oeeOption} height={300} />
        </Card>
        <Card title={t.analytics.pareto}>
          {paretoRows.length ? <Chart option={paretoOption} height={300} /> : <p className="text-slate-400">{t.common.noData}</p>}
        </Card>
      </div>

      <Card title={t.analytics.timeline}>
        <div className="mb-1 flex flex-wrap gap-3 text-xs text-slate-300">
          {STATE_ORDER.map((s) => (
            <span key={s} className="flex items-center gap-1">
              <span className="inline-block h-3 w-3 rounded-sm" style={{ backgroundColor: STATE_COLORS[s] }} />
              {stateLabel(s)}
            </span>
          ))}
        </div>
        <Chart option={ganttOption} height={90 + shown.length * 44} />
      </Card>

      <Card title={t.analytics.oeeByMachine}>
        <table className="w-full text-sm">
          <thead className="text-left text-slate-400">
            <tr>
              {[t.analytics.machine, t.analytics.planned, t.analytics.run, t.analytics.parts, t.analytics.scrap,
                t.analytics.availability, t.analytics.performance, t.analytics.quality, t.analytics.oee].map((h) => (
                <th key={h} className="px-2 py-1 font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((m) => {
              const row = oee.data?.find((r) => r.machine_id === m.id);
              return (
                <tr key={m.id} className="tabular border-t border-slate-800">
                  <td className="px-2 py-1">{m.name}</td>
                  <td className="px-2 py-1">{row ? formatDuration(row.planned_time_s) : "—"}</td>
                  <td className="px-2 py-1">{row ? formatDuration(row.run_time_s) : "—"}</td>
                  <td className="px-2 py-1">{row?.total_parts ?? "—"}</td>
                  <td className="px-2 py-1">{row?.scrap_parts ?? "—"}</td>
                  <td className="px-2 py-1">{percent(row?.availability, 1)}</td>
                  <td className="px-2 py-1">{row?.no_standard ? t.analytics.noStandard : percent(row?.performance, 1)}</td>
                  <td className="px-2 py-1">{percent(row?.quality, 1)}</td>
                  <td className="px-2 py-1 font-semibold">{percent(row?.oee, 1)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
