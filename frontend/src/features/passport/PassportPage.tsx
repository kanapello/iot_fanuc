import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { api, ApiError, getToken, query, type AlarmEpisode, type Interval, type Live, type Machine, type RepairRequest } from "../../api/client";
import { t } from "../../shared/i18n";
import { formatDuration, formatTime, percent, STATE_ORDER, stateColor, stateLabel } from "../../shared/states";
import { useNow } from "../../shared/useLive";
import { formatMoment, formatSpan, RequestStatusChip } from "../repairs/common";

// Phone first: one column, large type, touch targets of at least 48 px, nothing that needs hover.
const action = "min-h-[56px] rounded-xl px-4 text-lg font-semibold active:scale-[0.99] disabled:opacity-50";

interface Passport {
  machine_id: number;
  view: "machine" | "service" | "operator";
}

interface Annotation {
  start_ts: string;
  end_ts: string | null;
  reason_name: string;
}

/**
 * The page behind a QR sticker (/m/<code>). Without a session: the login form, then back here.
 * Then by role: master, engineer, administrator — this page (status, timeline, repair request);
 * service — the machine card; operator — the operator screen. A wrong code and a machine out of
 * reach give the same "no access".
 */
export function PassportPage() {
  const code = useParams().code ?? "";
  const signedIn = Boolean(getToken());
  const passport = useQuery({
    queryKey: ["passport", code],
    queryFn: () => api<Passport>(`/passport/${encodeURIComponent(code)}`),
    enabled: signedIn,
    retry: false,
  });
  if (!signedIn) return <Navigate to={`/login${query({ next: `/m/${code}` })}`} replace />;
  if (passport.isError) {
    const status = passport.error instanceof ApiError ? passport.error.status : 0;
    return <Message text={status === 404 || status === 403 ? t.common.noAccess : t.common.error} />;
  }
  if (!passport.data) return <Message text={t.common.loading} muted />;
  const { machine_id: machineId, view } = passport.data;
  if (view === "service") return <Navigate to={`/machines/${machineId}`} replace />;
  if (view === "operator") return <Navigate to={`/operator/${machineId}`} replace />;
  return <MachinePassport machineId={machineId} />;
}

function Message({ text, muted = false }: { text: string; muted?: boolean }) {
  return (
    <div className="flex min-h-full flex-col items-center justify-center gap-6 p-6 text-center">
      <p className={`text-2xl ${muted ? "text-slate-400" : "font-semibold"}`}>{text}</p>
      {!muted && (
        <Link to="/" className={`${action} flex items-center bg-slate-800`}>
          {t.passport.home}
        </Link>
      )}
    </div>
  );
}

function MachinePassport({ machineId }: { machineId: number }) {
  const { data: machine, isError } = useQuery({
    queryKey: ["machine", machineId],
    queryFn: () => api<Machine>(`/machines/${machineId}`),
    refetchInterval: 10_000,
  });
  if (isError) return <Message text={t.common.error} />;
  if (!machine) return <Message text={t.common.loading} muted />;
  const live = machine.live;
  return (
    <div className="mx-auto flex min-h-full max-w-xl flex-col gap-4 p-4 pb-10">
      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="break-words text-2xl font-bold leading-tight">{machine.name}</h1>
          <p className="text-sm text-slate-400">{[machine.site_name, machine.area_name].filter(Boolean).join(" · ")}</p>
        </div>
        <Link to="/" className="flex min-h-[48px] shrink-0 items-center rounded-xl bg-slate-800 px-4 text-base">
          {t.passport.home}
        </Link>
      </header>
      <StatusCard live={live} />
      {live?.shift && <ShiftTiles live={live} />}
      <TimelineCard machineId={machineId} live={live} />
      <RequestsCard machineId={machineId} />
    </div>
  );
}

function StatusCard({ live }: { live: Live | null }) {
  const now = useNow();
  const state = live?.state ?? "OFFLINE";
  const seconds = live?.since ? (now - Date.parse(live.since)) / 1000 : null;
  return (
    <section className="rounded-2xl p-5 text-white" style={{ backgroundColor: stateColor(state) }}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-4">
        <span className="text-4xl font-extrabold uppercase tracking-wide">{stateLabel(state)}</span>
        {seconds !== null && <span className="tabular text-3xl font-bold">{formatDuration(seconds)}</span>}
      </div>
      {live?.reason && (
        <div className="mt-1 text-lg font-semibold">
          {t.passport.reason}: {live.reason.name}
        </div>
      )}
      <div className="mt-2 text-base">
        {t.passport.program}: {live?.program ?? "—"}
      </div>
      <div className="text-sm opacity-90">
        {t.passport.lastData}: {live?.last_ts ? formatMoment(live.last_ts) : t.passport.noData}
      </div>
    </section>
  );
}

function ShiftTiles({ live }: { live: Live }) {
  const shift = live.shift!;
  const cycle = live.last_cycle;
  const tiles: [string, string, string?][] = [
    [t.passport.parts, `${shift.good_parts} / ${shift.plan_now ?? "—"}`],
    [t.passport.oee, percent(shift.oee)],
    [t.passport.scrap, String(shift.scrap_parts)],
    [
      t.passport.lastCycle,
      cycle ? formatDuration(cycle.duration_s) : "—",
      cycle ? `${t.passport.norm} ${cycle.norm_s != null ? formatDuration(cycle.norm_s) : "—"}` : undefined,
    ],
  ];
  return (
    <section className="space-y-2">
      <h2 className="text-sm text-slate-400">
        {t.passport.shift}: {shift.name} · {formatTime(shift.start)}–{formatTime(shift.end)}
      </h2>
      <div className="grid grid-cols-2 gap-3">
        {tiles.map(([label, value, hint]) => (
          <div key={label} className="rounded-xl bg-slate-800 p-3">
            <div className="text-xs uppercase text-slate-400">{label}</div>
            <div className="tabular text-2xl font-bold">{value}</div>
            {hint && <div className="tabular text-sm text-slate-300">{hint}</div>}
          </div>
        ))}
      </div>
    </section>
  );
}

type Range = "shift" | "h12" | "h24";

function TimelineCard({ machineId, live }: { machineId: number; live: Live | null }) {
  const shift = live?.shift ?? null;
  const [chosen, setChosen] = useState<Range | null>(null);
  const [selected, setSelected] = useState<number | null>(null); // start of the tapped segment, ms
  const now = Math.floor(useNow(60_000) / 60_000) * 60_000; // a new window once a minute
  const range: Range = chosen === "shift" && !shift ? "h12" : (chosen ?? (shift ? "shift" : "h12"));
  // the window: the current shift up to its end (what is still ahead stays empty), or the last hours
  const [start, end] =
    range === "shift" && shift ? [Date.parse(shift.start), Date.parse(shift.end)] : [now - (range === "h24" ? 24 : 12) * 3_600_000, now];
  const upTo = Math.min(end, now);
  const params = query({ machine_id: machineId, from: new Date(start).toISOString(), to: new Date(upTo).toISOString() });
  const { data } = useQuery({
    queryKey: ["passport-timeline", params],
    queryFn: () => api<{ intervals: Interval[]; annotations: Annotation[] }>(`/timeline${params}`),
    enabled: upTo > start,
    placeholderData: (previous) => previous,
  });

  const segments = useMemo(
    () =>
      (data?.intervals ?? [])
        .map((i) => ({ ...i, s: Math.max(Date.parse(i.start_ts), start), e: Math.min(Date.parse(i.end_ts), upTo) }))
        .filter((x) => x.e > x.s),
    [data, start, upTo],
  );
  const span = end - start;
  const totals = STATE_ORDER.map((state) => ({
    state,
    seconds: segments.filter((x) => x.state === state).reduce((sum, x) => sum + (x.e - x.s) / 1000, 0),
  })).filter((x) => x.seconds > 0);
  const elapsed = totals.reduce((sum, x) => sum + x.seconds, 0);
  const current = segments.find((x) => x.s === selected) ?? null;
  const reason = current
    ? data?.annotations.find((a) => Date.parse(a.start_ts) <= current.s && (a.end_ts === null || Date.parse(a.end_ts) > current.s))
    : undefined;

  const pick = (event: React.MouseEvent<HTMLDivElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    const at = start + ((event.clientX - box.left) / box.width) * span;
    const hit = segments.find((x) => x.s <= at && at < x.e);
    setSelected(hit ? hit.s : null);
  };
  const ranges: Range[] = shift ? ["shift", "h12", "h24"] : ["h12", "h24"];

  return (
    <section className="space-y-3 rounded-2xl bg-slate-900 p-4">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">{t.passport.timeline}</h2>
        <div className="flex gap-1 rounded-xl bg-slate-800 p-1">
          {ranges.map((r) => (
            <button
              key={r}
              className={`min-h-[44px] rounded-lg px-3 text-base font-medium ${r === range ? "bg-sky-600 text-white" : "text-slate-300"}`}
              onClick={() => {
                setChosen(r);
                setSelected(null);
              }}
            >
              {t.passport.ranges[r]}
            </button>
          ))}
        </div>
      </div>
      <div
        className="relative h-14 cursor-pointer overflow-hidden rounded-lg bg-slate-800"
        style={
          upTo < end
            ? { backgroundImage: "repeating-linear-gradient(135deg, transparent 0 6px, rgba(148,163,184,0.18) 6px 12px)" }
            : undefined
        }
        onClick={pick}
        role="img"
        aria-label={t.passport.timeline}
      >
        <div className="absolute inset-y-0 left-0 bg-slate-800" style={{ width: `${((upTo - start) / span) * 100}%` }} />
        {segments.map((x) => (
          <div
            key={x.s}
            className={`absolute inset-y-0 ${x.s === selected ? "z-10 outline outline-2 outline-white" : ""}`}
            style={{
              left: `${((x.s - start) / span) * 100}%`,
              width: `${((x.e - x.s) / span) * 100}%`,
              backgroundColor: stateColor(x.state),
            }}
          />
        ))}
      </div>
      <div className="tabular flex justify-between text-sm text-slate-400">
        <span>{formatTime(start)}</span>
        <span>{formatTime(start + span / 2)}</span>
        <span>{formatTime(end)}</span>
      </div>
      {current ? (
        <div className="rounded-xl bg-slate-800 p-3 text-base">
          <div className="flex items-center gap-2 font-semibold">
            <span className="inline-block h-4 w-4 rounded" style={{ backgroundColor: stateColor(current.state) }} />
            {stateLabel(current.state)} · {formatSpan((current.e - current.s) / 1000)}
          </div>
          <div className="tabular text-slate-300">
            {formatTime(current.s)}–{formatTime(current.e)}
            {current.program ? ` · ${t.passport.program} ${current.program}` : ""}
          </div>
          {reason && (
            <div className="text-slate-300">
              {t.passport.reason}: {reason.reason_name}
            </div>
          )}
        </div>
      ) : (
        <p className="text-sm text-slate-500">{t.passport.tapHint}</p>
      )}
      <div className="space-y-1 text-base">
        {totals.map((x) => (
          <div key={x.state} className="flex items-center gap-2">
            <span className="inline-block h-3 w-3 shrink-0 rounded-sm" style={{ backgroundColor: stateColor(x.state) }} />
            <span className="text-slate-300">{stateLabel(x.state)}</span>
            <span className="tabular ml-auto whitespace-nowrap">
              {formatSpan(x.seconds)} · {percent(elapsed ? x.seconds / elapsed : null)}
            </span>
          </div>
        ))}
        {!totals.length && <span className="text-slate-500">{t.common.noData}</span>}
      </div>
    </section>
  );
}

function RequestsCard({ machineId }: { machineId: number }) {
  const client = useQueryClient();
  const requests = useQuery({
    queryKey: ["passport-requests", machineId],
    queryFn: () => api<RepairRequest[]>(`/repair-requests${query({ machine_id: machineId, open: "true" })}`),
    refetchInterval: 60_000,
  });
  // an alarm that is still active and has no request yet: one tap opens a request from it
  const alarms = useQuery({
    queryKey: ["passport-alarms", machineId],
    queryFn: () =>
      api<AlarmEpisode[]>(`/alarms${query({ machine_id: machineId, from: new Date(Date.now() - 86_400_000).toISOString() })}`),
    refetchInterval: 30_000,
  });
  const activeAlarms = (alarms.data ?? []).filter((a) => a.active && a.request_id === null && a.code);
  const [open, setOpen] = useState(false);
  const [description, setDescription] = useState("");
  const [failureAt, setFailureAt] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const send = async (body: Record<string, unknown>) => {
    setBusy(true);
    setError("");
    try {
      const created = await api<{ id: number }>("/repair-requests", { method: "POST", body: { machine_id: machineId, ...body } });
      setNotice(`${t.passport.created}${created.id}`);
      setOpen(false);
      setDescription("");
      setFailureAt("");
      await client.invalidateQueries({ queryKey: ["passport-requests", machineId] });
      await client.invalidateQueries({ queryKey: ["passport-alarms", machineId] });
    } catch (e) {
      setError(e instanceof ApiError && e.status === 409 ? t.passport.exists : `${t.common.error}: ${e instanceof Error ? e.message : ""}`);
    } finally {
      setBusy(false);
    }
  };
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (description.trim().length < 3) {
      setError(t.passport.describeHint);
      return;
    }
    void send({ description: description.trim(), failure_ts: failureAt ? new Date(failureAt).toISOString() : null });
  };
  const field = "w-full rounded-xl border border-slate-600 bg-slate-800 px-3 py-3 text-lg outline-none focus:border-sky-500";

  return (
    <section className="space-y-3 rounded-2xl bg-slate-900 p-4">
      <h2 className="text-lg font-semibold">{t.passport.requests}</h2>
      {notice && <p className="rounded-xl bg-green-900/60 px-3 py-2 text-base text-green-100">{notice}</p>}
      {(requests.data ?? []).map((r) => (
        <div key={r.id} className="space-y-1 rounded-xl bg-slate-800 p-3">
          <div className="flex items-center gap-2">
            <span className="font-semibold">№{r.id}</span>
            <RequestStatusChip status={r.status} voided={Boolean(r.voided_at)} />
            <span className="tabular ml-auto text-sm text-slate-400">{formatMoment(r.failure_ts)}</span>
          </div>
          <p className="break-words text-base">{r.description}</p>
          {r.executor_name && <p className="text-sm text-slate-400">{r.executor_name}</p>}
        </div>
      ))}
      {requests.data && !requests.data.length && <p className="text-base text-slate-400">{t.passport.noRequests}</p>}
      {activeAlarms.map((a) => (
        <button
          key={`${a.start_ts}${a.code}`}
          className={`${action} w-full bg-purple-700 text-left text-white`}
          disabled={busy}
          onClick={() => void send({ alarm_ts: a.start_ts, alarm_code: a.code })}
        >
          {t.passport.fromAlarm} {a.code}
          {a.message ? ` · ${a.message}` : ""}
        </button>
      ))}
      {open ? (
        <form className="space-y-3" onSubmit={submit}>
          <label className="block space-y-1">
            <span className="text-sm text-slate-400">{t.passport.describe}</span>
            <textarea className={field} rows={3} value={description} onChange={(e) => setDescription(e.target.value)} autoFocus />
          </label>
          <label className="block space-y-1">
            <span className="text-sm text-slate-400">{t.passport.failureAt}</span>
            <input className={field} type="datetime-local" value={failureAt} onChange={(e) => setFailureAt(e.target.value)} />
          </label>
          {error && <p className="text-base text-red-300">{error}</p>}
          <div className="grid grid-cols-2 gap-3">
            <button type="button" className={`${action} bg-slate-700`} onClick={() => setOpen(false)}>
              {t.common.cancel}
            </button>
            <button className={`${action} bg-sky-600 text-white`} disabled={busy}>
              {t.passport.submit}
            </button>
          </div>
        </form>
      ) : (
        <>
          {error && <p className="text-base text-red-300">{error}</p>}
          <button
            className={`${action} w-full bg-sky-600 text-white`}
            onClick={() => {
              setOpen(true);
              setNotice("");
              setError("");
            }}
          >
            {t.passport.newRequest}
          </button>
        </>
      )}
    </section>
  );
}
