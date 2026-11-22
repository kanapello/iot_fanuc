import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Machine, type MaintenancePlan, type MaintenanceRecord, type Me } from "../../api/client";
import { t } from "../../shared/i18n";
import { button, formatDay, formatMoment, input, machinePlace, PlanStatusChip, primary } from "./common";

const hours = (value: number) => value.toLocaleString("ru-RU", { maximumFractionDigits: 1 });
const refresh = (client: ReturnType<typeof useQueryClient>) =>
  Promise.all([
    client.invalidateQueries({ queryKey: ["maintenance"] }),
    client.invalidateQueries({ queryKey: ["maintenance-records"] }),
  ]);

/** Plans of the machines, the most urgent first. */
export function useSchedule() {
  return useQuery({
    queryKey: ["maintenance"],
    queryFn: () => api<MaintenancePlan[]>("/maintenance"),
    select: (plans) => [...plans].sort((a, b) => b.used_share - a.used_share),
    refetchInterval: 60_000,
  });
}

function NewPlanForm({ onClose }: { onClose: () => void }) {
  const client = useQueryClient();
  const { data: machines } = useQuery({ queryKey: ["machines"], queryFn: () => api<Machine[]>("/machines") });
  const [machineId, setMachineId] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [intervalHours, setIntervalHours] = useState("");
  const [countedFrom, setCountedFrom] = useState("");
  const [error, setError] = useState("");
  const chosen = machineId ?? machines?.[0]?.id ?? null;
  const submit = async () => {
    setError("");
    try {
      await api("/maintenance-plans", {
        method: "POST",
        body: {
          machine_id: chosen,
          name,
          interval_run_hours: Number(intervalHours.replace(",", ".")),
          counted_from: countedFrom ? new Date(`${countedFrom}T00:00:00`).toISOString() : null,
        },
      });
      await refresh(client);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : t.common.error);
    }
  };
  return (
    <div className="space-y-2 rounded-lg border border-slate-700 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <select className={input} value={chosen ?? ""} onChange={(e) => setMachineId(Number(e.target.value))}>
          {(machines ?? []).map((m) => (
            <option key={m.id} value={m.id}>
              {m.name} · {m.org_name} · {m.site_name}
            </option>
          ))}
        </select>
        <input className={`${input} min-w-64 flex-1`} placeholder={t.maintenance.name} value={name} onChange={(e) => setName(e.target.value)} />
        <input className={`${input} w-36`} placeholder={t.maintenance.interval} inputMode="decimal" value={intervalHours} onChange={(e) => setIntervalHours(e.target.value)} />
        <label className="flex items-center gap-2 text-sm text-slate-300">
          {t.maintenance.countedFrom}
          <input type="date" className={input} value={countedFrom} onChange={(e) => setCountedFrom(e.target.value)} />
        </label>
      </div>
      {error && <p className="text-sm text-red-300">{error}</p>}
      <div className="flex gap-2">
        <button className={primary} onClick={submit}>
          {t.maintenance.create}
        </button>
        <button className={button} onClick={onClose}>
          {t.common.cancel}
        </button>
      </div>
    </div>
  );
}

function Records({ plan, manage }: { plan: MaintenancePlan; manage: boolean }) {
  const client = useQueryClient();
  const { data: records } = useQuery({
    queryKey: ["maintenance-records", plan.id],
    queryFn: () => api<MaintenanceRecord[]>(`/maintenance-plans/${plan.id}/records`),
  });
  const [error, setError] = useState("");
  if (!records) return <p className="text-sm text-slate-400">{t.common.loading}</p>;
  return (
    <div className="space-y-1 text-sm">
      {records.length === 0 && <p className="text-slate-400">{t.maintenance.noRecords}</p>}
      {records.map((r) => (
        <div key={r.id} className={`flex items-center gap-2 ${r.voided_at ? "text-slate-500" : ""}`}>
          <span className={r.voided_at ? "line-through" : ""}>
            {formatMoment(r.performed_at)} · {r.engineer}
            {r.comment ? ` · ${r.comment}` : ""}
          </span>
          {r.voided_at && <span className="rounded bg-slate-700 px-1.5 py-0.5 text-xs text-slate-300">{r.void_reason}</span>}
          {manage && !r.voided_at && (
            <button
              className="text-xs text-slate-400 hover:text-red-300 hover:underline"
              onClick={async () => {
                const reason = window.prompt(t.maintenance.voidReason);
                if (!reason) return;
                try {
                  await api(`/maintenance-records/${r.id}/void`, { method: "POST", body: { reason } });
                  await refresh(client);
                } catch (e) {
                  setError(e instanceof Error ? e.message : t.common.error);
                }
              }}
            >
              {t.maintenance.voidRecord}
            </button>
          )}
        </div>
      ))}
      {error && <p className="text-red-300">{error}</p>}
    </div>
  );
}

export function MaintenancePanel({ me }: { me: Me }) {
  const client = useQueryClient();
  const manage = me.menu.repairs_manage;
  const { data: plans, isLoading } = useSchedule();
  const [history, setHistory] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const run = async (action: () => Promise<unknown>) => {
    setError("");
    try {
      await action();
      await refresh(client);
    } catch (e) {
      setError(e instanceof Error ? e.message : t.common.error);
    }
  };
  return (
    <section className="space-y-3 rounded-xl bg-slate-900 p-4" id="maintenance">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-lg font-semibold">{t.maintenance.title}</h2>
        {manage && !creating && (
          <button className={`${primary} ml-auto`} onClick={() => setCreating(true)}>
            {t.maintenance.newPlan}
          </button>
        )}
      </div>
      <p className="text-xs text-slate-400">{t.maintenance.hint}</p>
      {creating && <NewPlanForm onClose={() => setCreating(false)} />}
      {error && <p className="text-sm text-red-300">{error}</p>}
      {isLoading && <p className="text-slate-400">{t.common.loading}</p>}
      {plans?.length === 0 && <p className="text-slate-400">{t.maintenance.none}</p>}
      {Boolean(plans?.length) && (
        <table className="w-full text-sm">
          <thead className="text-left text-slate-400">
            <tr>
              <th className="px-2 py-1">{t.repairs.machine}</th>
              <th className="px-2 py-1">{t.maintenance.plan}</th>
              <th className="px-2 py-1 text-right">{t.maintenance.interval}</th>
              <th className="px-2 py-1 text-right">{t.maintenance.runHours}</th>
              <th className="px-2 py-1 text-right">{t.maintenance.remaining}</th>
              <th className="px-2 py-1">{t.maintenance.forecast}</th>
              <th className="px-2 py-1">{t.maintenance.lastDone}</th>
              <th className="px-2 py-1" />
              <th className="px-2 py-1" />
            </tr>
          </thead>
          <tbody>
            {(plans ?? []).map((p) => (
              <PlanRows
                key={p.id}
                plan={p}
                manage={manage}
                open={history === p.id}
                onHistory={() => setHistory(history === p.id ? null : p.id)}
                run={run}
              />
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function PlanRows({
  plan: p,
  manage,
  open,
  onHistory,
  run,
}: {
  plan: MaintenancePlan;
  manage: boolean;
  open: boolean;
  onHistory: () => void;
  run: (action: () => Promise<unknown>) => Promise<void>;
}) {
  return (
    <>
      <tr className="tabular border-t border-slate-800 align-top">
        <td className="px-2 py-2">
          <div className="font-medium">{p.machine_name}</div>
          <div className="text-xs text-slate-400">{machinePlace(p)}</div>
        </td>
        <td className="px-2 py-2">{p.name}</td>
        <td className="px-2 py-2 text-right">{hours(p.interval_run_hours)}</td>
        <td className="px-2 py-2 text-right">{hours(p.run_hours)}</td>
        <td className={`px-2 py-2 text-right ${p.remaining_hours <= 0 ? "font-semibold text-red-300" : ""}`}>{hours(p.remaining_hours)}</td>
        <td className="whitespace-nowrap px-2 py-2">{p.status === "due" ? "—" : formatDay(p.forecast_at)}</td>
        <td className="whitespace-nowrap px-2 py-2">{formatDay(p.last_done_at)}</td>
        <td className="px-2 py-2">
          <PlanStatusChip status={p.status} />
        </td>
        <td className="whitespace-nowrap px-2 py-2 text-right">
          <span className="space-x-2">
            {manage && (
              <button
                className={primary}
                onClick={() => {
                  const comment = window.prompt(t.maintenance.markDonePrompt);
                  if (comment !== null) {
                    run(() => api(`/maintenance-plans/${p.id}/records`, { method: "POST", body: { comment: comment || null } }));
                  }
                }}
              >
                {t.maintenance.markDone}
              </button>
            )}
            <button className={button} onClick={onHistory}>
              {t.maintenance.records}
            </button>
            {manage && (
              <button
                className={button}
                onClick={() =>
                  window.confirm(`${t.maintenance.switchOff}: ${p.name}?`) &&
                  run(() =>
                    api(`/maintenance-plans/${p.id}`, {
                      method: "PUT",
                      body: { name: p.name, interval_run_hours: p.interval_run_hours, notes: p.notes, is_active: false },
                    }),
                  )
                }
              >
                {t.maintenance.switchOff}
              </button>
            )}
          </span>
        </td>
      </tr>
      {open && (
        <tr className="bg-slate-950/40">
          <td colSpan={9} className="px-3 py-2">
            <Records plan={p} manage={manage} />
          </td>
        </tr>
      )}
    </>
  );
}
