import { useQuery } from "@tanstack/react-query";
import type { EChartsOption } from "echarts";
import { useMemo } from "react";
import { api } from "../../api/client";
import { Chart } from "../../shared/Chart";
import { t } from "../../shared/i18n";
import { directionLabel } from "./common";

interface Trace {
  direction: string;
  points: number;
  angle_deg: number[];
  deviation_um: number[];
  source: string;
}

/** Radial deviation around the nominal circle, Ballbar-style: the dashed ring is the nominal radius. */
export function PolarPlot({ testId }: { testId: number }) {
  const { data: traces } = useQuery({
    queryKey: ["traces", testId],
    queryFn: () => api<Trace[]>(`/ballbar/tests/${testId}/traces?max_points=1500`),
  });
  const option = useMemo<EChartsOption>(() => {
    const all = (traces ?? []).flatMap((tr) => tr.deviation_um);
    const span = Math.max(1, ...all.map(Math.abs)) * 1.3;
    const nominal = Array.from({ length: 361 }, (_, a) => [0, a]);
    return {
      legend: { top: 0, data: (traces ?? []).map((tr) => directionLabel(tr.direction)) },
      tooltip: { trigger: "item", formatter: (p: any) => `${p.seriesName}: ${p.value[1].toFixed(1)}° · ${p.value[0].toFixed(2)} µm` },
      polar: { radius: "78%" },
      angleAxis: { type: "value", min: 0, max: 360, startAngle: 0, clockwise: false, interval: 45, axisLabel: { formatter: "{value}°" } },
      radiusAxis: { min: -span, max: span, axisLabel: { formatter: (v: number) => `${v.toFixed(1)}` }, name: "µm" },
      series: [
        { name: "0", type: "line", coordinateSystem: "polar", data: nominal, showSymbol: false, lineStyle: { type: "dashed", color: "#64748b", width: 1 }, silent: true },
        ...(traces ?? []).map((tr) => ({
          name: directionLabel(tr.direction),
          type: "line" as const,
          coordinateSystem: "polar" as const,
          showSymbol: false,
          lineStyle: { width: 1.5, color: tr.direction === "CW" ? "#38bdf8" : "#f87171" },
          itemStyle: { color: tr.direction === "CW" ? "#38bdf8" : "#f87171" },
          data: tr.angle_deg.map((angle, i) => [tr.deviation_um[i], ((angle % 360) + 360) % 360]),
        })),
      ],
    };
  }, [traces]);
  if (!traces?.length) return <p className="text-sm text-slate-400">{t.geometry.noTraces}</p>;
  return (
    <div>
      {traces.some((tr) => tr.source === "demo") && <p className="text-xs text-amber-400">{t.geometry.demoTrace}</p>}
      <Chart option={option} height={380} />
    </div>
  );
}
