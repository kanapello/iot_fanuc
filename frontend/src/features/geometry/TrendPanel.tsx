import { useQuery } from "@tanstack/react-query";
import type { EChartsOption } from "echarts";
import { useMemo, useState } from "react";
import { api, query, type Catalog, type Trend, type TrendGroup } from "../../api/client";
import { Chart } from "../../shared/Chart";
import { t } from "../../shared/i18n";
import { formatNumber, keysOf, resultLabel } from "./common";

const select = "rounded border border-slate-700 bg-slate-800 px-2 py-1 text-sm";
const EVENT_COLORS: Record<string, string> = { collision: "#a855f7", repair: "#22c55e" };

const groupLabel = (g: TrendGroup) =>
  `${g.template ? `${t.geometry.template} «${g.template}»` : `${g.plane} · R${formatNumber(g.radius_mm)} · F${formatNumber(g.feed_mm_min)} · ${g.arc_deg}°`} (${g.tests})`;

/** One parameter over comparable tests, with collision marks and completed repairs as vertical lines. */
export function TrendPanel({ machineId, catalog }: { machineId: number; catalog: Catalog }) {
  const { data: groups } = useQuery({
    queryKey: ["ballbar-groups", machineId],
    queryFn: () => api<TrendGroup[]>(`/machines/${machineId}/ballbar/groups`),
  });
  const [groupKey, setGroupKey] = useState("");
  const [key, setKey] = useState("squareness||");
  const group = groups?.find((g) => g.key === groupKey) ?? groups?.[0];
  const options = group
    ? catalog.parameters.flatMap((p) => keysOf(p, group.plane, catalog).map(([axis, direction]) => ({ code: p.code, axis, direction })))
    : [];
  const current = options.find((o) => `${o.code}|${o.axis ?? ""}|${o.direction ?? ""}` === key) ?? options[0];
  const params =
    group && current ? query({ group: group.key, parameter: current.code, axis: current.axis, direction: current.direction }) : "";
  const { data: trend } = useQuery({
    queryKey: ["ballbar-trend", machineId, params],
    queryFn: () => api<Trend>(`/machines/${machineId}/ballbar/trend${params}`),
    enabled: Boolean(params),
  });

  const option = useMemo<EChartsOption>(
    () => ({
      tooltip: { trigger: "axis" },
      grid: { left: 50, right: 30, top: 34, bottom: 30 },
      xAxis: { type: "time" },
      yAxis: { type: "value", name: trend?.unit },
      series: [
        {
          type: "line",
          name: current ? resultLabel(current.code, current.axis, current.direction, catalog) : "",
          data: (trend?.points ?? []).map((p) => [p.performed_at, p.value]),
          symbolSize: 9,
          itemStyle: { color: "#38bdf8" },
          lineStyle: { color: "#38bdf8" },
          markLine: {
            symbol: "none",
            silent: false,
            data: (trend?.events ?? []).map((e) => ({
              xAxis: e.ts,
              name: `${t.geometry.event[e.kind]}${e.text ? `: ${e.text}` : ""}`,
              lineStyle: { color: EVENT_COLORS[e.kind], type: "dashed", width: 1.5 },
              label: { formatter: t.geometry.event[e.kind], color: EVENT_COLORS[e.kind], position: "insideEndTop" },
            })),
            tooltip: { formatter: (p: any) => `${p.name}<br/>${new Date(p.value).toLocaleDateString("ru-RU")}` },
          },
        },
      ],
    }),
    [trend, current, catalog],
  );

  if (!groups?.length) return null;
  return (
    <div className="space-y-2 rounded-xl bg-slate-900 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="mr-2 font-semibold">{t.geometry.trend}</h3>
        <select className={select} value={group?.key ?? ""} onChange={(e) => setGroupKey(e.target.value)}>
          {groups.map((g) => (
            <option key={g.key} value={g.key}>
              {groupLabel(g)}
            </option>
          ))}
        </select>
        <select className={select} value={key} onChange={(e) => setKey(e.target.value)}>
          {options.map((o) => {
            const value = `${o.code}|${o.axis ?? ""}|${o.direction ?? ""}`;
            return (
              <option key={value} value={value}>
                {resultLabel(o.code, o.axis, o.direction, catalog)}
              </option>
            );
          })}
        </select>
        <span className="text-xs text-slate-400">{t.geometry.trendHint}</span>
      </div>
      {trend && trend.points.length ? <Chart option={option} height={280} /> : <p className="text-sm text-slate-400">{t.common.noData}</p>}
      <div className="flex gap-4 text-xs text-slate-400">
        {Object.entries(EVENT_COLORS).map(([kind, color]) => (
          <span key={kind} className="flex items-center gap-1">
            <span className="inline-block h-0 w-5 border-t-2 border-dashed" style={{ borderColor: color }} />
            {t.geometry.event[kind]}
          </span>
        ))}
      </div>
    </div>
  );
}
