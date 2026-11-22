import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, query, type Machine, type Me, type RepairRequest } from "../../api/client";
import { t } from "../../shared/i18n";
import { button, formatMoment, formatSpan, input, localInputNow, machinePlace, primary, RequestStatusChip } from "./common";

const refreshRequests = (client: ReturnType<typeof useQueryClient>) =>
  Promise.all([
    client.invalidateQueries({ queryKey: ["repair-requests"] }),
    client.invalidateQueries({ queryKey: ["repair-request"] }),
    client.invalidateQueries({ queryKey: ["alarms"] }),
  ]);

function NewRequestForm({ onClose }: { onClose: () => void }) {
  const client = useQueryClient();
  const { data: machines } = useQuery({ queryKey: ["machines"], queryFn: () => api<Machine[]>("/machines") });
  const [machineId, setMachineId] = useState<number | null>(null);
  const [description, setDescription] = useState("");
  const [failureAt, setFailureAt] = useState(localInputNow());
  const [error, setError] = useState("");
  const chosen = machineId ?? machines?.[0]?.id ?? null;
  const submit = async () => {
    setError("");
    try {
      await api("/repair-requests", {
        method: "POST",
        body: { machine_id: chosen, description, failure_ts: new Date(failureAt).toISOString() },
      });
      await refreshRequests(client);
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
              {m.name} · {m.site_name}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-2 text-sm text-slate-300">
          {t.repairs.failureAt}
          <input type="datetime-local" className={input} value={failureAt} onChange={(e) => setFailureAt(e.target.value)} />
        </label>
      </div>
      <textarea
        className={`${input} h-20 w-full`}
        placeholder={t.repairs.description}
        value={description}
        onChange={(e) => setDescription(e.target.value)}
      />
      {error && <p className="text-sm text-red-300">{error}</p>}
      <div className="flex gap-2">
        <button className={primary} onClick={submit}>
          {t.repairs.create}
        </button>
        <button className={button} onClick={onClose}>
          {t.common.cancel}
        </button>
      </div>
    </div>
  );
}

/** The next step of a request for the service; withdrawal for the customer while nobody has taken it. */
function RequestActions({ request, me }: { request: RepairRequest; me: Me }) {
  const client = useQueryClient();
  const manage = me.menu.repairs_manage;
  const { data: engineers } = useQuery({
    queryKey: ["service-engineers"],
    queryFn: () => api<{ id: number; name: string }[]>("/service-engineers"),
    enabled: manage,
    staleTime: 300_000,
  });
  const [executor, setExecutor] = useState<number | "">("");
  const [error, setError] = useState("");
  const run = async (action: () => Promise<unknown>) => {
    setError("");
    try {
      await action();
      await refreshRequests(client);
    } catch (e) {
      setError(e instanceof Error ? e.message : t.common.error);
    }
  };
  const status = (body: Record<string, unknown>) => api(`/repair-requests/${request.id}/status`, { method: "POST", body });
  const voided = Boolean(request.voided_at);
  const canVoid = !voided && request.status !== "done" && (manage || request.status === "new");
  const executorSelect = (
    <select className={input} value={executor} onChange={(e) => setExecutor(e.target.value ? Number(e.target.value) : "")}>
      <option value="">{t.repairs.chooseExecutor}</option>
      {(engineers ?? []).map((e) => (
        <option key={e.id} value={e.id}>
          {e.name}
        </option>
      ))}
    </select>
  );

  if (voided || (!manage && !canVoid)) return null;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {manage && request.status === "new" && (
          <>
            {executorSelect}
            <button className={primary} disabled={executor === ""} onClick={() => run(() => status({ status: "accepted", executor_id: executor }))}>
              {t.repairs.accept}
            </button>
          </>
        )}
        {manage && request.status === "accepted" && (
          <button className={primary} onClick={() => run(() => status({ status: "in_progress" }))}>
            {t.repairs.start}
          </button>
        )}
        {manage && request.status === "in_progress" && (
          <button
            className={primary}
            onClick={() => {
              const workDone = window.prompt(t.repairs.workDonePrompt);
              if (workDone) run(() => status({ status: "done", work_done: workDone }));
            }}
          >
            {t.repairs.complete}
          </button>
        )}
        {manage && (request.status === "accepted" || request.status === "in_progress") && (
          <>
            {executorSelect}
            <button
              className={button}
              disabled={executor === ""}
              onClick={() => run(() => status({ status: request.status, executor_id: executor }))}
            >
              {t.repairs.reassign}
            </button>
          </>
        )}
        {canVoid && (
          <button
            className={`${button} ml-auto`}
            onClick={() => {
              const reason = window.prompt(t.repairs.voidReason);
              if (reason) run(() => api(`/repair-requests/${request.id}/void`, { method: "POST", body: { reason } }));
            }}
          >
            {t.repairs.void}
          </button>
        )}
      </div>
      {error && <p className="text-sm text-red-300">{error}</p>}
    </div>
  );
}

function RequestDetail({ id, me }: { id: number; me: Me }) {
  const { data: request } = useQuery({
    queryKey: ["repair-request", id],
    queryFn: () => api<RepairRequest>(`/repair-requests/${id}`),
  });
  if (!request) return <p className="text-sm text-slate-400">{t.common.loading}</p>;
  return (
    <div className="space-y-3">
      <div className="grid gap-x-6 gap-y-1 text-sm md:grid-cols-2">
        <div>
          <span className="text-slate-400">{t.repairs.description}: </span>
          {request.description}
        </div>
        <div>
          <span className="text-slate-400">{t.repairs.createdBy}: </span>
          {request.created_by_name}, {formatMoment(request.created_at)}
        </div>
        {request.alarm_code && (
          <div>
            <span className="text-slate-400">{t.repairs.fromAlarm}: </span>
            {request.alarm_code} {request.alarm_message ?? ""}, {formatMoment(request.alarm_ts)}
          </div>
        )}
        {request.work_done && (
          <div>
            <span className="text-slate-400">{t.repairs.workDone}: </span>
            {request.work_done}
          </div>
        )}
        {request.void_reason && (
          <div>
            <span className="text-slate-400">{t.repairs.voided}: </span>
            {request.void_reason}
          </div>
        )}
      </div>
      <p className="text-xs text-slate-400">{t.repairs.timesHint}</p>
      <div>
        <h4 className="mb-1 text-sm font-semibold text-slate-300">{t.repairs.history}</h4>
        <ul className="space-y-0.5 text-sm">
          {(request.history ?? []).map((h, i) => (
            <li key={i}>
              <span className="tabular text-slate-400">{formatMoment(h.ts)}</span> · {t.repairs.actions[h.action] ?? h.action} · {h.user_name}
              {h.comment ? ` · ${h.comment}` : ""}
            </li>
          ))}
        </ul>
      </div>
      <RequestActions request={request} me={me} />
    </div>
  );
}

function restorationText(request: RepairRequest) {
  if (request.voided_at) return "—";
  if (request.restoration_s === null) return t.repairs.notRestored;
  return request.restoration_s === 0 ? t.repairs.noDowntime : formatSpan(request.restoration_s);
}

export function RequestsPanel({ me, focus }: { me: Me; focus: number | null }) {
  const [openOnly, setOpenOnly] = useState(focus === null);
  const [expanded, setExpanded] = useState<number | null>(focus);
  const [creating, setCreating] = useState(false);
  const requests = useQuery({
    queryKey: ["repair-requests", openOnly],
    queryFn: () => api<RepairRequest[]>(`/repair-requests${query({ open: openOnly ? "true" : undefined })}`),
    refetchInterval: 30_000,
  });
  const tab = (active: boolean) => `rounded px-3 py-1 text-sm ${active ? "bg-slate-700 text-white" : "text-slate-300 hover:bg-slate-800"}`;
  return (
    <section className="space-y-3 rounded-xl bg-slate-900 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="mr-2 text-lg font-semibold">{t.repairs.requests}</h2>
        <button className={tab(openOnly)} onClick={() => setOpenOnly(true)}>
          {t.repairs.open}
        </button>
        <button className={tab(!openOnly)} onClick={() => setOpenOnly(false)}>
          {t.repairs.all}
        </button>
        {!creating && (
          <button className={`${primary} ml-auto`} onClick={() => setCreating(true)}>
            {t.repairs.newRequest}
          </button>
        )}
      </div>
      {creating && <NewRequestForm onClose={() => setCreating(false)} />}
      {requests.isLoading && <p className="text-slate-400">{t.common.loading}</p>}
      {requests.data?.length === 0 && <p className="text-slate-400">{t.repairs.none}</p>}
      {Boolean(requests.data?.length) && (
        <table className="w-full text-sm">
          <thead className="text-left text-slate-400">
            <tr>
              <th className="px-2 py-1">{t.repairs.number}</th>
              <th className="px-2 py-1">{t.repairs.machine}</th>
              <th className="px-2 py-1">{t.repairs.description}</th>
              <th className="px-2 py-1">{t.repairs.status}</th>
              <th className="px-2 py-1">{t.repairs.failure}</th>
              <th className="px-2 py-1">{t.repairs.executor}</th>
              <th className="px-2 py-1">{t.repairs.reaction}</th>
              <th className="px-2 py-1">{t.repairs.restoration}</th>
            </tr>
          </thead>
          <tbody>
            {(requests.data ?? []).map((r) => (
              <RequestRow key={r.id} request={r} me={me} expanded={expanded === r.id} onToggle={() => setExpanded(expanded === r.id ? null : r.id)} />
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function RequestRow({ request: r, me, expanded, onToggle }: { request: RepairRequest; me: Me; expanded: boolean; onToggle: () => void }) {
  const voided = Boolean(r.voided_at);
  return (
    <>
      <tr className={`cursor-pointer border-t border-slate-800 align-top hover:bg-slate-800 ${voided ? "text-slate-500" : ""}`} onClick={onToggle}>
        <td className="tabular px-2 py-2">{r.id}</td>
        <td className="px-2 py-2">
          <div className="font-medium">{r.machine_name}</div>
          <div className="text-xs text-slate-400">{machinePlace(r)}</div>
        </td>
        <td className={`px-2 py-2 ${voided ? "line-through" : ""}`}>
          {r.alarm_code && !r.description.startsWith(r.alarm_code) && (
            <span className="mr-1 rounded bg-purple-800 px-1.5 py-0.5 text-xs">{r.alarm_code}</span>
          )}
          {r.description}
        </td>
        <td className="px-2 py-2">
          <RequestStatusChip status={r.status} voided={voided} />
        </td>
        <td className="tabular whitespace-nowrap px-2 py-2">{formatMoment(r.failure_ts)}</td>
        <td className="px-2 py-2">{r.executor_name ?? "—"}</td>
        <td className="tabular whitespace-nowrap px-2 py-2">{r.reaction_s === null ? (voided ? "—" : t.repairs.waiting) : formatSpan(r.reaction_s)}</td>
        <td className="tabular whitespace-nowrap px-2 py-2">{restorationText(r)}</td>
      </tr>
      {expanded && (
        <tr className="border-t border-slate-800 bg-slate-950/40">
          <td colSpan={8} className="px-3 py-3">
            <RequestDetail id={r.id} me={me} />
          </td>
        </tr>
      )}
    </>
  );
}
