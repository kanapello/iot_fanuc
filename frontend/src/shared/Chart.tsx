import * as echarts from "echarts";
import { useEffect, useRef } from "react";

/** Minimal ECharts wrapper: re-applies the option and follows container size. */
export function Chart({ option, height = 320 }: { option: echarts.EChartsOption; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts>();

  useEffect(() => {
    if (!ref.current) return;
    chart.current = echarts.init(ref.current, "dark");
    const observer = new ResizeObserver(() => chart.current?.resize());
    observer.observe(ref.current);
    return () => {
      observer.disconnect();
      chart.current?.dispose();
    };
  }, []);

  useEffect(() => {
    chart.current?.setOption({ backgroundColor: "transparent", ...option }, true);
  }, [option]);

  return <div ref={ref} style={{ height }} />;
}
