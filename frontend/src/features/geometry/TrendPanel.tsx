import { useQuery } from "@tanstack/react-query";
import type { EChartsOption } from "echarts";
import { useMemo, useState } from "react";
import { api, query, type Catalog, type GeometryStatus } from "../../api/client";
import { Chart } from "../../shared/Chart";
import { t } from "../../shared/i18n";
import { formatNumber, keysOf, resultLabel } from "./common";

interface Trend {
  unit: string;
  limit: number | null;
  limit_source: string | null;
  points: { test_id: number; performed_at: string; value: number; exceeded: boolean }[];
}

const select = "rounded border border-slate-700 bg-slate-800 px-2 py-1 text-sm";

export function TrendPanel({ machineId, status, catalog }: { machineId: number; status: GeometryStatus; catalog: Catalog }) {
  const setups = status.configurations;
  const [setupIndex, setSetupIndex] = useState(0);
  const [key, setKey] = useState("squareness||");
  const setup = setups[setupIndex];
  const options = setup
    ? catalog.parameters.flatMap((p) => keysOf(p, setup.plane, catalog).map(([axis, direction]) => ({ code: p.code, axis, direction })))
    : [];
  const current = options.find((o) => `${o.code}|${o.axis ?? ""}|${o.direction ?? ""}` === key) ?? options[0];
  const params = setup && current
    ? query({
        parameter: current.code,
        axis: current.axis,
        direction: current.direction,
        plane: setup.plane,
        radius_mm: setup.radius_mm,
        feed_mm_min: setup.feed_mm_min,
      })
    : "";
  const { data: trend } = useQuery({
    queryKey: ["ballbar-trend", machineId, params],
    queryFn: () => api<Trend>(`/machines/${machineId}/ballbar/trend${params}`),
    enabled: Boolean(params),
  });
  const option = useMemo<EChartsOption>(() => {
    const limit = trend?.limit;
    return {
      tooltip: { trigger: "axis" },
      grid: { left: 50, right: 30, top: 34, bottom: 30 },
      xAxis: { type: "time" },
      yAxis: { type: "value", name: trend?.unit },
      series: [
        {
          type: "line",
          name: current ? resultLabel(current.code, current.axis, current.direction, catalog) : "",
          data: (trend?.points ?? []).map((p) => ({
            value: [p.performed_at, p.value],
            itemStyle: { color: p.exceeded ? "#ef4444" : "#38bdf8" },
          })),
          symbolSize: 9,
          lineStyle: { color: "#38bdf8" },
          markLine: limit
            ? { symbol: "none", lineStyle: { color: "#ef4444", type: "dashed" }, data: [{ yAxis: limit }, { yAxis: -limit }], label: { formatter: "{c}" } }
            : undefined,
        },
      ],
    };
  }, [trend, current, catalog]);

  if (!setups.length) return null;
  return (
    <div className="space-y-2 rounded-xl bg-slate-900 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="mr-2 font-semibold">{t.geometry.trend}</h3>
        <select className={select} value={setupIndex} onChange={(e) => setSetupIndex(Number(e.target.value))}>
          {setups.map((s, i) => (
            <option key={i} value={i}>
              {s.plane} · R{formatNumber(s.radius_mm)} · F{formatNumber(s.feed_mm_min)} ({s.tests})
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
      {trend && trend.points.length ? <Chart option={option} height={260} /> : <p className="text-sm text-slate-400">{t.common.noData}</p>}
    </div>
  );
}
