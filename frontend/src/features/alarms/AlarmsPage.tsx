import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { EChartsOption } from "echarts";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, query, type AlarmContext, type AlarmEpisode, type AlarmFrequency } from "../../api/client";
import { Chart } from "../../shared/Chart";
import { t } from "../../shared/i18n";
import { NodePicker, useMe, useNodeSelection } from "../../shared/session";
import { STATE_COLORS } from "../../shared/states";
import { button, formatMoment, formatSpan, machinePlace, primary } from "../repairs/common";

const PERIOD_DAYS: Record<string, number> = { day: 1, week: 7, month: 30 };
const episodeKey = (e: AlarmEpisode) => `${e.machine_id}|${e.start_ts}|${e.code}`;

/** Telemetry of the 10 minutes before an alarm: spindle load, speed and feed, with the alarm moment marked. */
function AlarmContextView({ episode }: { episode: AlarmEpisode }) {
  const { data, isLoading } = useQuery({
    queryKey: ["alarm-context", episode.machine_id, episode.start_ts],
    queryFn: () => api<AlarmContext>(`/machines/${episode.machine_id}/alarm-context${query({ ts: episode.start_ts })}`),
  });
  const option = useMemo<EChartsOption | null>(() => {
    if (!data?.telemetry.length) return null;
    const series = (name: string, field: "spindle_load" | "spindle_speed" | "feed_actual", axis: number, color: string) => ({
      name,
      type: "line" as const,
      step: "end" as const,
      showSymbol: false,
      yAxisIndex: axis,
      color,
      data: data.telemetry.map((s) => [s.ts, s[field]]),
    });
    return {
      tooltip: { trigger: "axis" },
      legend: { textStyle: { color: "#cbd5e1" } },
      grid: { left: 50, right: 70, top: 40, bottom: 30 },
      xAxis: { type: "time", min: data.from, max: data.to },
      yAxis: [
        { type: "value", name: "%", min: 0 },
        { type: "value", name: "", splitLine: { show: false } },
      ],
      series: [
        {
          ...series(t.alarms.spindleLoad, "spindle_load", 0, "#f97316"),
          markLine: {
            symbol: "none",
            label: { formatter: t.alarms.alarmMoment, color: STATE_COLORS.ALARM },
            lineStyle: { color: STATE_COLORS.ALARM, width: 2 },
            data: [{ xAxis: episode.start_ts }],
          },
        },
        series(t.alarms.spindleSpeed, "spindle_speed", 1, "#38bdf8"),
        series(t.alarms.feed, "feed_actual", 1, "#a3e635"),
      ],
    };
  }, [data, episode.start_ts]);

  if (isLoading) return <p className="text-sm text-slate-400">{t.common.loading}</p>;
  if (!data || !option) return <p className="text-sm text-slate-400">{t.alarms.noTelemetry}</p>;
  const first = data.telemetry[0];
  const others = data.events.filter((e) => !(e.type === "alarm_raised" && e.ts === episode.start_ts && e.code === episode.code));
  return (
    <div className="space-y-2">
      <h4 className="text-sm font-semibold text-slate-300">{t.alarms.before}</h4>
      <Chart option={option} height={260} />
      <p className="text-sm text-slate-300">
        {t.alarms.stateAtStart}: {first.mode ?? "—"} · {first.run ?? "—"} · {first.program ?? "—"}
      </p>
      <div className="text-sm">
        <span className="text-slate-400">{t.alarms.events}: </span>
        {others.length === 0
          ? t.alarms.noEvents
          : others.map((e, i) => (
              <span key={i} className="mr-3">
                {formatMoment(e.ts)} {t.alarms.eventTypes[e.type] ?? e.type} {e.code ?? ""} {e.message ?? ""}
              </span>
            ))}
      </div>
    </div>
  );
}

function FrequencyTable({ rows }: { rows: AlarmFrequency[] }) {
  const shown = rows.filter((r) => r.count > 0);
  return (
    <section className="rounded-xl bg-slate-900 p-4">
      <h2 className="mb-2 text-lg font-semibold">{t.alarms.frequency}</h2>
      {shown.length === 0 ? (
        <p className="text-slate-400">{t.alarms.none}</p>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-slate-400">
            <tr>
              <th className="px-2 py-1">{t.alarms.machine}</th>
              <th className="px-2 py-1 text-right">{t.alarms.count}</th>
              <th className="px-2 py-1 text-right">{t.alarms.perDay}</th>
              <th className="px-2 py-1 text-right">{t.alarms.total}</th>
              <th className="px-2 py-1">{t.alarms.topCodes}</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.machine_id} className="tabular border-t border-slate-800 align-top">
                <td className="px-2 py-2">
                  <div className="font-medium">{r.machine_name}</div>
                  <div className="text-xs text-slate-400">{machinePlace(r)}</div>
                </td>
                <td className="px-2 py-2 text-right font-semibold">{r.count}</td>
                <td className="px-2 py-2 text-right">{r.per_day?.toLocaleString("ru-RU", { maximumFractionDigits: 1 }) ?? "—"}</td>
                <td className="whitespace-nowrap px-2 py-2 text-right">{formatSpan(r.duration_s)}</td>
                <td className="px-2 py-2">
                  {r.codes.slice(0, 4).map((c) => (
                    <span key={c.code ?? ""} className="mr-3 whitespace-nowrap" title={c.message ?? ""}>
                      <span className="rounded bg-purple-800 px-1.5 py-0.5 text-xs">{c.code ?? "—"}</span> ×{c.count}
                    </span>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

export function AlarmsPage() {
  const client = useQueryClient();
  const { data: me } = useMe();
  const selection = useNodeSelection(me?.sites);
  // a link from a notification: /alarms?machine=<id>&at=<start of the alarm> opens that machine and episode
  const [search, setSearch] = useSearchParams();
  const machineFilter = Number(search.get("machine")) || null;
  const at = search.get("at");
  const [period, setPeriod] = useState(at && Date.now() - Date.parse(at) > 7 * 86_400_000 ? "month" : "week");
  const [open, setOpen] = useState<string | null>(null);
  const opened = useRef(false);
  const [error, setError] = useState("");
  const from = useMemo(() => new Date(Date.now() - PERIOD_DAYS[period] * 86_400_000).toISOString(), [period]);
  const params = machineFilter
    ? query({ from, machine_id: machineFilter })
    : query({ from, site_id: selection.siteId, area_id: selection.areaId });
  const episodes = useQuery({
    queryKey: ["alarms", params],
    queryFn: () => api<AlarmEpisode[]>(`/alarms${params}`),
    refetchInterval: 30_000,
    enabled: Boolean(me?.menu.alarms),
  });
  const frequency = useQuery({
    queryKey: ["alarms", "frequency", params],
    queryFn: () => api<AlarmFrequency[]>(`/alarms/frequency${params}`),
    refetchInterval: 60_000,
    enabled: Boolean(me?.menu.alarms),
  });
  useEffect(() => {
    if (opened.current || !at || !episodes.data?.length) return;
    const target = Date.parse(at);
    const hit = episodes.data.find((e) => Math.abs(Date.parse(e.start_ts) - target) < 60_000) ?? episodes.data[0];
    setOpen(episodeKey(hit));
    opened.current = true;
  }, [episodes.data, at]);
  if (!me) return <p className="text-slate-400">{t.common.loading}</p>;
  if (!me.menu.alarms) return <p className="text-slate-400">{t.common.noAccess}</p>;

  const openRequest = async (e: AlarmEpisode) => {
    setError("");
    try {
      await api("/repair-requests", { method: "POST", body: { machine_id: e.machine_id, alarm_ts: e.start_ts, alarm_code: e.code } });
      await Promise.all([client.invalidateQueries({ queryKey: ["alarms"] }), client.invalidateQueries({ queryKey: ["repair-requests"] })]);
    } catch (err) {
      setError(err instanceof Error ? err.message : t.common.error);
    }
  };
  const tab = (active: boolean) => `rounded px-3 py-2 text-sm ${active ? "bg-slate-700 text-white" : "text-slate-300 hover:bg-slate-800"}`;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="mr-2 text-xl font-semibold">{t.alarms.title}</h1>
        {Object.keys(PERIOD_DAYS).map((p) => (
          <button key={p} className={tab(period === p)} onClick={() => setPeriod(p)}>
            {t.alarms.periods[p]}
          </button>
        ))}
        {machineFilter ? (
          <button className={`${button} ml-auto`} onClick={() => setSearch({})}>
            {episodes.data?.[0]?.machine_name ?? t.alarms.machine} ✕
          </button>
        ) : (
          <div className="ml-auto">
            <NodePicker sites={me.sites} selection={selection} />
          </div>
        )}
      </div>
      {error && <p className="rounded bg-red-900/60 px-3 py-2 text-sm text-red-200">{error}</p>}
      <FrequencyTable rows={frequency.data ?? []} />
      <section className="rounded-xl bg-slate-900 p-4">
        <h2 className="mb-2 text-lg font-semibold">{t.alarms.episodes}</h2>
        {episodes.isLoading && <p className="text-slate-400">{t.common.loading}</p>}
        {episodes.data?.length === 0 && <p className="text-slate-400">{t.alarms.none}</p>}
        {Boolean(episodes.data?.length) && (
          <table className="w-full text-sm">
            <thead className="text-left text-slate-400">
              <tr>
                <th className="px-2 py-1">{t.alarms.start}</th>
                <th className="px-2 py-1">{t.alarms.machine}</th>
                <th className="px-2 py-1">{t.alarms.code}</th>
                <th className="px-2 py-1">{t.alarms.message}</th>
                <th className="px-2 py-1 text-right">{t.alarms.duration}</th>
                <th className="px-2 py-1" />
              </tr>
            </thead>
            <tbody>
              {(episodes.data ?? []).map((e) => {
                const key = episodeKey(e);
                return (
                  <AlarmRow
                    key={key}
                    episode={e}
                    expanded={open === key}
                    canRequest={me.menu.repairs}
                    onToggle={() => setOpen(open === key ? null : key)}
                    onRequest={() => openRequest(e)}
                  />
                );
              })}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function AlarmRow({
  episode: e,
  expanded,
  canRequest,
  onToggle,
  onRequest,
}: {
  episode: AlarmEpisode;
  expanded: boolean;
  canRequest: boolean;
  onToggle: () => void;
  onRequest: () => void;
}) {
  return (
    <>
      <tr className="tabular border-t border-slate-800 align-top">
        <td className="whitespace-nowrap px-2 py-2">{formatMoment(e.start_ts)}</td>
        <td className="px-2 py-2">
          <div className="font-medium">{e.machine_name}</div>
          <div className="text-xs text-slate-400">{machinePlace(e)}</div>
        </td>
        <td className="px-2 py-2">
          <span className="rounded bg-purple-800 px-1.5 py-0.5 text-xs">{e.code ?? "—"}</span>
        </td>
        <td className="px-2 py-2">{e.message ?? "—"}</td>
        <td className="whitespace-nowrap px-2 py-2 text-right">
          {formatSpan(e.duration_s)}
          {e.active && <span className="ml-2 rounded bg-purple-600 px-1.5 py-0.5 text-xs text-white">{t.alarms.active}</span>}
        </td>
        <td className="whitespace-nowrap px-2 py-2 text-right">
          <span className="space-x-2">
            <button className={button} onClick={onToggle}>
              {expanded ? t.alarms.hide : t.alarms.telemetry}
            </button>
            {e.request_id !== null ? (
              <Link className="text-sky-400 hover:underline" to={`/repairs?request=${e.request_id}`}>
                {t.alarms.request}
                {e.request_id}
              </Link>
            ) : (
              canRequest && (
                <button className={primary} onClick={onRequest}>
                  {t.alarms.createRequest}
                </button>
              )
            )}
          </span>
        </td>
      </tr>
      {expanded && (
        <tr className="bg-slate-950/40">
          <td colSpan={6} className="px-3 py-3">
            <AlarmContextView episode={e} />
          </td>
        </tr>
      )}
    </>
  );
}
