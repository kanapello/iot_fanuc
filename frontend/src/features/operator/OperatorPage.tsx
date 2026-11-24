import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { api, type LastCycle, type Machine, type Reason } from "../../api/client";
import { t } from "../../shared/i18n";
import { formatDuration, percent, stateColor, stateLabel } from "../../shared/states";
import { useLive, useNow } from "../../shared/useLive";

// Touch targets for gloves: every button is at least 64 px high.
const bigButton = "min-h-[72px] rounded-2xl px-6 py-4 text-2xl font-bold active:scale-[0.98] disabled:opacity-50";

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-2xl bg-slate-800 p-5">
      <div className="text-xl font-semibold uppercase text-slate-400">{label}</div>
      <div className="tabular mt-1 text-5xl font-bold">{value}</div>
      {hint && <div className="tabular mt-1 text-2xl text-slate-300">{hint}</div>}
    </div>
  );
}

/** "4:32 / 4:00": the last cycle (part to part) and its norm; the hint tells the machining time. */
function lastCycleMetric(cycle: LastCycle | null): { value: string; hint?: string } {
  if (!cycle) return { value: "—" };
  const norm = cycle.norm_s != null ? formatDuration(cycle.norm_s) : "—";
  const kind = cycle.kind === "normal" ? "" : ` · ${t.operator.cycleKinds[cycle.kind]}`;
  return { value: `${formatDuration(cycle.duration_s)} / ${norm}`, hint: `${t.operator.machining} ${formatDuration(cycle.machine_s)}${kind}` };
}

function ReasonPicker({ reasons, onPick, onClose }: { reasons: Reason[]; onPick: (id: number) => void; onClose?: () => void }) {
  const [group, setGroup] = useState<Reason | null>(null);
  const items = group ? (group.children ?? []) : reasons;
  return (
    <div className="rounded-3xl bg-slate-800 p-6">
      <div className="mb-4 flex items-center justify-between gap-4">
        <h2 className="text-3xl font-bold">{group ? group.name : t.operator.reasonTitle}</h2>
        {(group || onClose) && (
          <button className={`${bigButton} bg-slate-600`} onClick={() => (group ? setGroup(null) : onClose?.())}>
            {group ? t.common.back : t.common.cancel}
          </button>
        )}
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {items.map((item) => (
          <button
            key={item.id}
            className={`${bigButton} min-h-[96px] text-left text-white`}
            style={{ backgroundColor: item.color }}
            onClick={() => (group ? onPick(item.id) : setGroup(item))}
          >
            {item.name}
          </button>
        ))}
      </div>
    </div>
  );
}

function ScrapPanel({ machineId, program, onDone }: { machineId: number; program: string | null; onDone: () => void }) {
  const [qty, setQty] = useState(1);
  const [busy, setBusy] = useState(false);
  const { data: reasons } = useQuery({
    queryKey: ["scrap-reasons", machineId],
    queryFn: () => api<{ id: number; name: string }[]>(`/scrap-reasons?machine_id=${machineId}`),
  });
  const save = async (reasonId: number) => {
    setBusy(true);
    try {
      await api("/scrap", { method: "POST", body: { machine_id: machineId, qty, reason_id: reasonId, program } });
      onDone();
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="rounded-3xl bg-slate-800 p-6">
      <div className="mb-4 flex items-center justify-between gap-4">
        <h2 className="text-3xl font-bold">{t.operator.scrapTitle}</h2>
        <button className={`${bigButton} bg-slate-600`} onClick={onDone}>
          {t.common.cancel}
        </button>
      </div>
      <div className="mb-5 flex items-center gap-4">
        <span className="text-2xl text-slate-300">{t.operator.scrapQty}</span>
        <button className={`${bigButton} w-24 bg-slate-600`} onClick={() => setQty((q) => Math.max(1, q - 1))}>
          −
        </button>
        <span className="tabular w-24 text-center text-6xl font-bold">{qty}</span>
        <button className={`${bigButton} w-24 bg-slate-600`} onClick={() => setQty((q) => q + 1)}>
          +
        </button>
        <button className={`${bigButton} bg-slate-600`} onClick={() => setQty((q) => q + 5)}>
          +5
        </button>
      </div>
      <div className="mb-2 text-2xl text-slate-300">{t.operator.scrapReason}</div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {(reasons ?? []).map((reason) => (
          <button key={reason.id} disabled={busy} className={`${bigButton} bg-rose-700 text-left`} onClick={() => save(reason.id)}>
            {reason.name}
          </button>
        ))}
      </div>
    </div>
  );
}

export function OperatorPage() {
  const machineId = Number(useParams().machineId);
  const { data: machine, isError } = useQuery({
    queryKey: ["machine", machineId],
    queryFn: () => api<Machine>(`/machines/${machineId}`),
  });
  const { data: reasons } = useQuery({
    queryKey: ["reasons", machineId],
    queryFn: () => api<Reason[]>(`/reasons?machine_id=${machineId}`),
  });
  const { machines, connected } = useLive();
  const now = useNow();
  const [panel, setPanel] = useState<"none" | "reason" | "scrap" | "collision">("none");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const live = machines[machineId] ?? machine?.live ?? null;
  if (isError) return <p className="p-8 text-3xl">{t.operator.notFound}</p>;
  if (!machine || !live) return <p className="p-8 text-3xl text-slate-400">{t.common.loading}</p>;

  const state = live.state;
  const seconds = live.since ? (now - Date.parse(live.since)) / 1000 : 0;
  const isDown = state !== "RUN";
  // The reason is requested automatically once the stop is long enough to become STOP (or on alarm).
  const needsReason = (state === "STOP" || state === "ALARM") && !live.reason;
  const shift = live.shift;

  const pickReason = async (reasonId: number) => {
    await api("/annotations", {
      method: "POST",
      body: { machine_id: machineId, start_ts: live.downtime_since ?? live.since, reason_id: reasonId },
    });
    setPanel("none");
  };
  const markCollision = async () => {
    setBusy(true);
    try {
      await api(`/machines/${machineId}/collisions`, { method: "POST", body: {} });
      setNotice(t.geometry.collisionSaved);
      setPanel("none");
    } finally {
      setBusy(false);
    }
  };
  const toggleSetup = async () => {
    setBusy(true);
    try {
      await api(`/machines/${machineId}/setup`, { method: "POST", body: { active: !live.setup_active } });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-full flex-col gap-5 p-5">
      {!connected && (
        <div className="rounded-xl bg-amber-500 px-4 py-3 text-xl font-semibold text-black">{t.common.connectionLost}</div>
      )}
      <header className="flex items-baseline justify-between gap-4">
        <h1 className="text-4xl font-bold">{machine.name}</h1>
        <span className="text-2xl text-slate-400">{shift ? `${t.operator.shift}: ${shift.name}` : ""}</span>
      </header>

      <section className="rounded-3xl p-8 text-white" style={{ backgroundColor: stateColor(state) }}>
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="text-7xl font-extrabold uppercase tracking-wide">{stateLabel(state)}</div>
          <div className="tabular text-7xl font-bold">{formatDuration(seconds)}</div>
        </div>
        {isDown && live.reason && (
          <div className="mt-3 text-3xl font-semibold">
            {t.operator.reasonSet}: {live.reason.name}
          </div>
        )}
      </section>

      {notice && <div className="rounded-xl bg-purple-800 px-5 py-3 text-2xl font-semibold">{notice}</div>}
      {panel === "scrap" ? (
        <ScrapPanel machineId={machineId} program={live.program} onDone={() => setPanel("none")} />
      ) : panel === "collision" ? (
        <section className="space-y-5 rounded-3xl bg-slate-800 p-6">
          <h2 className="text-3xl font-bold">{t.geometry.confirmCollision}</h2>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <button className={`${bigButton} min-h-[96px] bg-purple-700 text-white`} disabled={busy} onClick={markCollision}>
              {t.geometry.collisionYes}
            </button>
            <button className={`${bigButton} min-h-[96px] bg-slate-600 text-white`} onClick={() => setPanel("none")}>
              {t.common.cancel}
            </button>
          </div>
        </section>
      ) : needsReason || panel === "reason" ? (
        <ReasonPicker reasons={reasons ?? []} onPick={pickReason} onClose={needsReason ? undefined : () => setPanel("none")} />
      ) : (
        <section className="grid grid-cols-2 gap-5 xl:grid-cols-4">
          <Metric label={t.operator.program} value={live.program ?? "—"} />
          <Metric
            label={`${t.operator.fact} / ${t.operator.plan}`}
            value={shift ? `${shift.good_parts} / ${shift.plan_now ?? "—"}` : "—"}
          />
          <Metric label={t.operator.planShift} value={shift?.plan_shift != null ? String(shift.plan_shift) : "—"} />
          <Metric label={t.operator.oee} value={percent(shift?.oee)} />
          <Metric label={t.operator.lastCycle} {...lastCycleMetric(live.last_cycle)} />
        </section>
      )}

      <footer className="mt-auto grid grid-cols-2 gap-5 xl:grid-cols-4">
        <button
          className={`${bigButton} bg-slate-700 text-white`}
          disabled={!isDown}
          onClick={() => setPanel(panel === "reason" ? "none" : "reason")}
        >
          {live.reason ? t.operator.changeReason : t.operator.setReason}
        </button>
        <button className={`${bigButton} bg-rose-700 text-white`} onClick={() => setPanel(panel === "scrap" ? "none" : "scrap")}>
          {t.operator.scrap}
          {shift && shift.scrap_parts > 0 ? ` (${shift.scrap_parts})` : ""}
        </button>
        <button
          className={`${bigButton} text-white`}
          style={{ backgroundColor: live.setup_active ? stateColor("SETUP") : "#334155" }}
          disabled={busy}
          onClick={toggleSetup}
        >
          {live.setup_active ? t.operator.setupOff : t.operator.setupOn}
        </button>
        <button className={`${bigButton} bg-purple-800 text-white`} onClick={() => setPanel(panel === "collision" ? "none" : "collision")}>
          {t.geometry.collision}
        </button>
      </footer>
    </div>
  );
}
