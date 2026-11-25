import { useQuery } from "@tanstack/react-query";
import type { EChartsOption } from "echarts";
import { useMemo, useState, type ReactNode } from "react";
import { api, query, type Live, type Machine, type Overview, type OverviewCard, type OverviewMetric, type OverviewPeriod } from "../../api/client";
import { Chart } from "../../shared/Chart";
import { t } from "../../shared/i18n";
import { useShellNode } from "../../shared/Shell";
import { STATE_COLORS, STATE_ORDER, percent, stateLabel } from "../../shared/states";
import { useLive } from "../../shared/useLive";
import { ChangePp } from "./ChangePp";
import { MachinePanel } from "./MachinePanel";

const PERIODS: OverviewPeriod[] = ["shift", "today", "yesterday", "7d", "30d"];
const METRICS: OverviewMetric[] = ["utilization", "oee", "performance", "availability"];
const PERIOD_KEY = "mm_overview_period";
export const COLORS = { current: "#38bdf8", previous: "#94a3b8", usual: "#e2e8f0", good: "#34d399", scrap: "#f87171" };

const pct = (v: number | null) => (v === null ? null : Math.round(v * 1000) / 10);

function readPeriod(): OverviewPeriod {
  try {
    const value = localStorage.getItem(PERIOD_KEY) as OverviewPeriod | null;
    return value && PERIODS.includes(value) ? value : "today";
  } catch {
    return "today";
  }
}

export function Panel({ title, hint, children, className = "" }: { title: string; hint?: string; children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-xl border border-slate-800 bg-slate-900 p-4 ${className}`}>
      <h2 className="text-base font-semibold">{title}</h2>
      {hint && <p className="text-xs text-slate-400">{hint}</p>}
      <div className="mt-3">{children}</div>
    </section>
  );
}

function Legend() {
  const item = (color: string, dash: string, label: string) => (
    <span className="flex items-center gap-1.5">
      <svg width="22" height="6" aria-hidden>
        <line x1="0" y1="3" x2="22" y2="3" stroke={color} strokeWidth="2" strokeDasharray={dash} />
      </svg>
      {label}
    </span>
  );
  return (
    <div className="flex flex-wrap gap-4 text-xs text-slate-400">
      {item(COLORS.current, "", t.overview.current)}
      {item(COLORS.previous, "5 3", t.overview.previous)}
      {item(COLORS.usual, "1 3", t.overview.usual)}
    </div>
  );
}

function KpiCard({ metric, card, labels, comparable }: { metric: OverviewMetric; card: OverviewCard; labels: string[]; comparable: boolean }) {
  const option = useMemo<EChartsOption>(() => {
    const line = (name: string, data: (number | null)[], color: string, type: "solid" | "dashed" | "dotted", width: number) => ({
      name,
      type: "line" as const,
      data: data.map(pct),
      showSymbol: false,
      connectNulls: false,
      lineStyle: { color, type, width },
      itemStyle: { color },
    });
    return {
      tooltip: { trigger: "axis", valueFormatter: (v) => (v == null ? "—" : `${v}%`) },
      grid: { left: 36, right: 8, top: 10, bottom: 22 },
      xAxis: { type: "category", data: labels, axisLabel: { fontSize: 10, color: "#64748b" }, boundaryGap: false },
      yAxis: { type: "value", min: 0, axisLabel: { formatter: "{value}%", fontSize: 10, color: "#64748b" }, splitLine: { lineStyle: { color: "#1e293b" } } },
      series: [
        line(t.overview.usual, card.usual_series, COLORS.usual, "dotted", 1.5),
        line(t.overview.previous, card.previous_series, COLORS.previous, "dashed", 1.5),
        line(t.overview.current, card.series, COLORS.current, "solid", 2.5),
      ],
    };
  }, [card, labels]);
  return (
    <section className="rounded-xl border border-slate-800 bg-slate-900 p-4" title={t.overview.cardHints[metric]}>
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-sm font-medium text-slate-300">{t.overview.cards[metric]}</h2>
        <ChangePp change={card.change} comparable={comparable} />
      </div>
      <div className="tabular mt-1 text-3xl font-bold">{percent(card.value, 1)}</div>
      <Chart option={option} height={130} />
    </section>
  );
}

function Donut({ data, center, sub }: { data: { name: string; value: number; color: string }[]; center: string; sub?: string }) {
  const option = useMemo<EChartsOption>(
    () => ({
      tooltip: { trigger: "item" },
      series: [
        {
          type: "pie",
          radius: ["62%", "86%"],
          avoidLabelOverlap: false,
          label: { show: false },
          silent: data.every((d) => !d.value),
          data: data.some((d) => d.value)
            ? data.map((d) => ({ name: d.name, value: d.value, itemStyle: { color: d.color } }))
            : [{ name: "", value: 1, itemStyle: { color: "#1e293b" } }],
        },
      ],
    }),
    [data],
  );
  return (
    <div className="relative">
      <Chart option={option} height={150} />
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
        <span className="tabular text-2xl font-bold">{center}</span>
        {sub && <span className="text-xs text-slate-400">{sub}</span>}
      </div>
    </div>
  );
}

function OutputCard({ output, comparable }: { output: Overview["output"]; comparable: boolean }) {
  const change = output.change;
  return (
    <Panel title={t.overview.output}>
      <div className="grid grid-cols-2 items-center gap-2">
        <Donut
          data={[
            { name: t.overview.good, value: output.good, color: COLORS.good },
            { name: t.overview.scrap, value: output.scrap, color: COLORS.scrap },
          ]}
          center={String(output.total)}
          sub={t.overview.partsTotal}
        />
        <div className="space-y-2 text-sm">
          <Row color={COLORS.good} label={t.overview.good} value={output.good} />
          <Row color={COLORS.scrap} label={t.overview.scrap} value={output.scrap} />
          <div className="pt-1">
            {change === null || !comparable ? (
              <span className="text-xs text-slate-500">{t.overview.noCompare}</span>
            ) : (
              <span className={`tabular text-sm font-semibold ${change >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                {change >= 0 ? "▲ +" : "▼ "}
                {(change * 100).toFixed(1)}% <span className="font-normal text-slate-400">{t.overview.vsPrevious}</span>
              </span>
            )}
          </div>
        </div>
      </div>
    </Panel>
  );
}

function Row({ color, label, value, onClick }: { color: string; label: string; value: ReactNode; onClick?: () => void }) {
  return (
    <div className={`flex items-center justify-between gap-2 ${onClick ? "cursor-pointer hover:text-white" : ""}`} onClick={onClick}>
      <span className="flex items-center gap-2">
        <span className="inline-block h-3 w-3 rounded-sm" style={{ backgroundColor: color }} />
        {label}
      </span>
      <span className="tabular font-semibold">{value}</span>
    </div>
  );
}

function LiveCard({ machines, live, connected, onPick }: { machines: Machine[]; live: Record<number, Live>; connected: boolean; onPick: (id: number) => void }) {
  const stateOf = (m: Machine) => live[m.id]?.state ?? m.live?.state ?? "OFFLINE";
  const counts = Object.fromEntries(STATE_ORDER.map((s) => [s, machines.filter((m) => stateOf(m) === s).length]));
  return (
    <Panel title={t.overview.machinesNow}>
      {!connected && <p className="mb-2 text-xs text-amber-400">{t.overview.liveOffline}</p>}
      <div className="grid grid-cols-2 items-center gap-2">
        <Donut
          data={STATE_ORDER.map((s) => ({ name: stateLabel(s), value: counts[s], color: STATE_COLORS[s] }))}
          center={String(machines.length)}
          sub={t.nav.machines.toLowerCase()}
        />
        <div className="space-y-1 text-sm">
          {STATE_ORDER.map((s) => (
            <Row key={s} color={STATE_COLORS[s]} label={stateLabel(s)} value={counts[s]} />
          ))}
        </div>
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        {machines.map((m) => (
          <button
            key={m.id}
            onClick={() => onPick(m.id)}
            className="rounded-md px-2 py-1 text-xs font-semibold text-white"
            style={{ backgroundColor: STATE_COLORS[stateOf(m)] ?? STATE_COLORS.OFFLINE }}
            title={stateLabel(stateOf(m))}
          >
            {m.name}
          </button>
        ))}
      </div>
    </Panel>
  );
}

const hours = (seconds: number) => `${(seconds / 3600).toFixed(1)} ${t.overview.hours}`;

function DowntimeCard({ downtime }: { downtime: Overview["downtime"] }) {
  const unexplained = downtime.unexplained;
  const rows = [
    ...downtime.reasons.map((r) => ({ key: `r${r.reason_id}`, label: r.name, total: r.duration_s, parts: [{ color: r.color ?? "#64748b", value: r.duration_s, title: r.group_name ?? r.name }] })),
    ...(downtime.other_reasons_s > 0
      ? [{ key: "other", label: t.overview.otherReasons, total: downtime.other_reasons_s, parts: [{ color: "#475569", value: downtime.other_reasons_s, title: t.overview.otherReasons }] }]
      : []),
    {
      key: "none",
      label: t.overview.withoutReason,
      total: unexplained.total_s,
      parts: Object.entries(unexplained.by_state)
        .filter(([, v]) => v > 0)
        .map(([state, value]) => ({ color: STATE_COLORS[state], value, title: `${stateLabel(state)}: ${hours(value)}` })),
    },
  ];
  const max = Math.max(...rows.map((r) => r.total), 1);
  return (
    <Panel title={t.overview.downtime}>
      <div className="space-y-2.5">
        {rows.map((r) => (
          <div key={r.key}>
            <div className="flex justify-between gap-2 text-sm">
              <span className="truncate">{r.label}</span>
              <span className="tabular shrink-0 text-slate-300">{hours(r.total)}</span>
            </div>
            <div className="mt-1 flex h-2.5 overflow-hidden rounded bg-slate-800" style={{ width: `${Math.max((r.total / max) * 100, 0.5)}%` }}>
              {r.parts.map((p, i) => (
                <div key={i} title={p.title} style={{ width: `${(p.value / (r.total || 1)) * 100}%`, backgroundColor: p.color }} />
              ))}
            </div>
          </div>
        ))}
      </div>
      {unexplained.total_s > 0 && (
        <div className="mt-3 flex flex-wrap gap-3 text-xs text-slate-400">
          {Object.entries(unexplained.by_state)
            .filter(([, v]) => v > 0)
            .map(([state, value]) => (
              <span key={state} className="flex items-center gap-1">
                <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: STATE_COLORS[state] }} />
                {stateLabel(state)} {hours(value)}
              </span>
            ))}
        </div>
      )}
    </Panel>
  );
}

/** A ring of utilization with a tick at the usual level. */
function Ring({ value, usual }: { value: number; usual: number }) {
  const r = 20;
  const c = 2 * Math.PI * r;
  const angle = usual * 2 * Math.PI - Math.PI / 2;
  return (
    <svg viewBox="0 0 52 52" className="h-14 w-14 shrink-0" aria-hidden>
      <circle cx="26" cy="26" r={r} fill="none" stroke="#1e293b" strokeWidth="6" />
      <circle
        cx="26"
        cy="26"
        r={r}
        fill="none"
        stroke={COLORS.current}
        strokeWidth="6"
        strokeDasharray={`${Math.max(value, 0) * c} ${c}`}
        transform="rotate(-90 26 26)"
      />
      <line
        x1={26 + Math.cos(angle) * 14}
        y1={26 + Math.sin(angle) * 14}
        x2={26 + Math.cos(angle) * 26}
        y2={26 + Math.sin(angle) * 26}
        stroke={COLORS.usual}
        strokeWidth="2.5"
      />
      <text x="26" y="29.5" textAnchor="middle" fontSize="10" fontWeight="700" fill="#f8fafc">
        {Math.round(value * 100)}%
      </text>
    </svg>
  );
}

function BelowUsualCard({ data, onPick }: { data: Overview; onPick: (id: number) => void }) {
  return (
    <Panel title={t.overview.belowUsual} hint={t.overview.belowUsualHint.replace("{pp}", String(data.settings.utilization_drop_pp))}>
      {!data.below_usual.length && <p className="text-sm text-slate-400">{t.overview.belowUsualEmpty}</p>}
      <ul className="divide-y divide-slate-800">
        {data.below_usual.map((row) => (
          <li key={row.machine_id}>
            <button className="flex w-full items-center gap-3 py-2 text-left hover:bg-slate-800/50" onClick={() => onPick(row.machine_id)}>
              <Ring value={row.utilization} usual={row.usual} />
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium">{row.name}</span>
                <span className="text-xs text-slate-400">
                  {t.overview.usual}: {percent(row.usual)}
                </span>
              </span>
              <ChangePp change={row.change} />
            </button>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

function SlowCyclesCard({ data, onPick }: { data: Overview; onPick: (id: number) => void }) {
  const max = Math.max(...data.slow_cycles.map((r) => Math.max(r.median_s, r.norm_s)), 1);
  return (
    <Panel title={t.overview.slowCycles} hint={t.overview.slowCyclesHint.replace("{pct}", String(data.settings.slow_cycle_pct))}>
      {!data.slow_cycles.length && <p className="text-sm text-slate-400">{t.overview.slowCyclesEmpty}</p>}
      <ul className="divide-y divide-slate-800">
        {data.slow_cycles.map((row) => (
          <li key={`${row.machine_id}:${row.program}`}>
            <button className="block w-full py-2 text-left hover:bg-slate-800/50" onClick={() => onPick(row.machine_id)}>
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate font-medium">
                  {row.name} · <span className="text-slate-300">{row.program ?? "—"}</span>
                </span>
                <span className="tabular shrink-0 text-sm font-semibold text-red-400">
                  +{row.diff_s.toFixed(0)} {t.units.s} (+{row.diff_pct.toFixed(1)}%)
                </span>
              </div>
              <div className="mt-1.5 space-y-1 text-xs text-slate-400">
                {[
                  [t.overview.fact, row.median_s, COLORS.scrap],
                  [t.overview.norm, row.norm_s, COLORS.previous],
                ].map(([label, value, color]) => (
                  <div key={label as string} className="flex items-center gap-2">
                    <span className="w-10 shrink-0">{label}</span>
                    <div className="h-2 rounded" style={{ width: `${((value as number) / max) * 75}%`, backgroundColor: color as string }} />
                    <span className="tabular">
                      {(value as number).toFixed(0)} {t.units.s}
                    </span>
                  </div>
                ))}
              </div>
              <div className="mt-0.5 text-xs text-slate-500">
                {t.overview.perPart} · {row.cycles} {t.overview.cycles}
              </div>
            </button>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

export function OverviewPage() {
  const { siteId, areaId } = useShellNode();
  const [period, setPeriodState] = useState<OverviewPeriod>(readPeriod);
  const [picked, setPicked] = useState<number | null>(null);
  const setPeriod = (value: OverviewPeriod) => {
    setPeriodState(value);
    try {
      localStorage.setItem(PERIOD_KEY, value);
    } catch {
      /* not remembered */
    }
  };

  const params = query({ period, site_id: siteId, area_id: areaId });
  const overview = useQuery({
    queryKey: ["overview", params],
    queryFn: () => api<Overview>(`/overview${params}`),
    enabled: siteId !== null,
    refetchInterval: 60_000,
    placeholderData: (previous) => previous,
  });
  const { data: machines } = useQuery({ queryKey: ["machines"], queryFn: () => api<Machine[]>("/machines") });
  const { machines: live, connected } = useLive();
  const shown = (machines ?? []).filter((m) => m.site_id === siteId && (!areaId || m.area_id === areaId));
  const data = overview.data;
  const labels = data?.buckets.map((b) => b.label) ?? [];
  const pickedMachine = data?.machines.find((m) => m.machine_id === picked);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap rounded-lg bg-slate-900 p-1">
          {PERIODS.map((p) => (
            <button
              key={p}
              onClick={() => setPeriod(p)}
              className={`rounded-md px-3 py-1.5 text-sm font-medium ${period === p ? "bg-sky-600 text-white" : "text-slate-300 hover:bg-slate-800"}`}
            >
              {t.overview.periods[p]}
            </button>
          ))}
        </div>
        {data && (
          <span className="text-xs text-slate-400">
            {data.period.shift_name ? `${t.overview.shift} «${data.period.shift_name}»: ` : ""}
            {new Date(data.period.from).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short", timeZone: data.period.timezone })} –{" "}
            {new Date(data.period.to).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short", timeZone: data.period.timezone })}
          </span>
        )}
        <span className="ml-auto">
          <Legend />
        </span>
      </div>

      {!data && <p className="text-slate-400">{overview.isError ? t.common.error : t.common.loading}</p>}
      {data && (
        <>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {METRICS.map((metric) => (
              <KpiCard key={metric} metric={metric} card={data.cards[metric]} labels={labels} comparable={data.period.comparable} />
            ))}
          </div>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <OutputCard output={data.output} comparable={data.period.comparable} />
            <LiveCard machines={shown} live={live} connected={connected} onPick={setPicked} />
            <DowntimeCard downtime={data.downtime} />
          </div>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <BelowUsualCard data={data} onPick={setPicked} />
            <SlowCyclesCard data={data} onPick={setPicked} />
          </div>
        </>
      )}
      {data && pickedMachine && (
        <MachinePanel machine={pickedMachine} overview={data} live={live[pickedMachine.machine_id]} onClose={() => setPicked(null)} />
      )}
    </div>
  );
}
