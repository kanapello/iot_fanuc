import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, api, type BallbarTest, type GeometryStatus, type Machine } from "../../api/client";
import { t } from "../../shared/i18n";
import { useMe } from "../../shared/session";
import { stateColor, stateLabel } from "../../shared/states";
import { canEditGeometry, exceededText, formatDate, formatNumber, shortTriggers, STATUS_COLORS, useCatalog } from "./common";
import { TestDetail } from "./TestDetail";
import { TestForm } from "./TestForm";
import { TolerancePanel } from "./TolerancePanel";
import { TrendPanel } from "./TrendPanel";

const input = "rounded border border-slate-600 bg-slate-800 px-2 py-1 text-sm";

function NextCheckCard({ machineId, status, editable }: { machineId: number; status: GeometryStatus; editable: boolean }) {
  const client = useQueryClient();
  const check = status.next_check;
  const policy = status.policy;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({
    interval_days: policy.interval_days?.toString() ?? "",
    interval_run_hours: policy.interval_run_hours?.toString() ?? "",
    on_alarm: policy.on_alarm,
    on_collision: policy.on_collision,
    alarm_patterns: policy.alarm_patterns.join(", "),
  });
  const [error, setError] = useState("");
  const save = async () => {
    try {
      await api(`/machines/${machineId}/ballbar/policy`, {
        method: "PUT",
        body: {
          interval_days: draft.interval_days ? Number(draft.interval_days) : null,
          interval_run_hours: draft.interval_run_hours ? Number(draft.interval_run_hours) : null,
          on_alarm: draft.on_alarm,
          on_collision: draft.on_collision,
          alarm_patterns: draft.alarm_patterns.split(",").map((p) => p.trim()).filter(Boolean),
        },
      });
      setEditing(false);
      await client.invalidateQueries({ queryKey: ["geometry-status", machineId] });
    } catch (e) {
      setError(e instanceof Error ? e.message : t.common.error);
    }
  };
  return (
    <div className="space-y-2 rounded-xl bg-slate-900 p-4">
      <div className="flex items-center gap-3">
        <h3 className="font-semibold">{t.geometry.nextCheck}</h3>
        <span className="rounded px-2 py-0.5 text-sm font-semibold text-white" style={{ backgroundColor: STATUS_COLORS[check.status] }}>
          {t.geometry.status[check.status]}
        </span>
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        <dt className="text-slate-400">{t.geometry.lastTest}</dt>
        <dd>{formatDate(check.last_test_at)}</dd>
        {check.due_date && (
          <>
            <dt className="text-slate-400">{t.geometry.dueDate}</dt>
            <dd>{formatDate(check.due_date)}</dd>
          </>
        )}
        {check.run_hours_since !== null && (
          <>
            <dt className="text-slate-400">{t.geometry.runHours}</dt>
            <dd className="tabular">{formatNumber(check.run_hours_since)}</dd>
            <dt className="text-slate-400">{t.geometry.hoursLeft}</dt>
            <dd className="tabular">{formatNumber(check.run_hours_left)}</dd>
          </>
        )}
      </dl>
      {check.triggers.length > 0 && (
        <div className="text-sm">
          <div className="text-slate-400">{t.geometry.triggers}</div>
          {shortTriggers(check.triggers).shown.map((tr, i) => (
            <div key={i} className="text-purple-300">
              {new Date(tr.ts).toLocaleString("ru-RU")} · {t.geometry.trigger[tr.kind]} {tr.code ?? ""} {tr.message ?? ""}
            </div>
          ))}
          {shortTriggers(check.triggers).more > 0 && (
            <div className="text-purple-300">
              {t.geometry.moreTriggers} {shortTriggers(check.triggers).more}
            </div>
          )}
        </div>
      )}
      <div className="text-xs text-slate-400">
        {t.geometry.policy}: {policy.interval_days ? `${policy.interval_days} дн.` : ""}
        {policy.interval_run_hours ? ` · ${policy.interval_run_hours} ч` : ""}
        {policy.on_alarm ? ` · ${t.geometry.trigger.alarm.toLowerCase()} (${policy.alarm_patterns.join(", ")})` : ""}
        {policy.on_collision ? ` · ${t.geometry.trigger.collision.toLowerCase()}` : ""}
        {policy.is_default ? ` — ${t.geometry.defaultPolicy}` : ""}
        {editable && !editing && (
          <button className="ml-2 text-sky-400 hover:underline" onClick={() => setEditing(true)}>
            {t.common.edit}
          </button>
        )}
      </div>
      {editing && (
        <div className="space-y-2 text-sm">
          {error && <p className="text-red-300">{error}</p>}
          <label className="flex items-center gap-2">
            {t.geometry.intervalDays}
            <input className={`${input} w-24`} value={draft.interval_days} onChange={(e) => setDraft({ ...draft, interval_days: e.target.value })} />
          </label>
          <label className="flex items-center gap-2">
            {t.geometry.intervalHours}
            <input className={`${input} w-24`} value={draft.interval_run_hours} onChange={(e) => setDraft({ ...draft, interval_run_hours: e.target.value })} />
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={draft.on_alarm} onChange={(e) => setDraft({ ...draft, on_alarm: e.target.checked })} />
            {t.geometry.onAlarm}
          </label>
          <label className="block">
            <span className="text-slate-400">{t.geometry.alarmPatterns}</span>
            <input className={`${input} w-full`} value={draft.alarm_patterns} onChange={(e) => setDraft({ ...draft, alarm_patterns: e.target.value })} />
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={draft.on_collision} onChange={(e) => setDraft({ ...draft, on_collision: e.target.checked })} />
            {t.geometry.onCollision}
          </label>
          <div className="space-x-2">
            <button className="rounded bg-sky-600 px-3 py-1 hover:bg-sky-500" onClick={save}>
              {t.common.save}
            </button>
            <button className="rounded bg-slate-700 px-3 py-1 hover:bg-slate-600" onClick={() => setEditing(false)}>
              {t.common.cancel}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function CollisionsCard({ machineId }: { machineId: number }) {
  const client = useQueryClient();
  const { data: marks } = useQuery({
    queryKey: ["ballbar-collisions", machineId],
    queryFn: () => api<{ id: number; ts: string; comment: string | null; operator: string | null }[]>(`/machines/${machineId}/collisions`),
  });
  const [comment, setComment] = useState("");
  const [notice, setNotice] = useState("");
  const mark = async () => {
    if (!window.confirm(t.geometry.confirmCollision)) return;
    try {
      await api(`/machines/${machineId}/collisions`, { method: "POST", body: { comment: comment || null } });
      setComment("");
      setNotice(t.geometry.collisionSaved);
      await client.invalidateQueries({ predicate: (q) => ["ballbar-collisions", "geometry-status"].includes(String(q.queryKey[0])) });
    } catch (e) {
      setNotice(e instanceof Error ? e.message : t.common.error);
    }
  };
  return (
    <div className="space-y-2 rounded-xl bg-slate-900 p-4">
      <h3 className="font-semibold">{t.geometry.collisions}</h3>
      {(marks ?? []).slice(0, 5).map((m) => (
        <div key={m.id} className="text-sm">
          {new Date(m.ts).toLocaleString("ru-RU")} · {m.comment ?? "—"} {m.operator ? `(${m.operator})` : ""}
        </div>
      ))}
      <div className="flex gap-2">
        <input className={`${input} flex-1`} placeholder={t.geometry.collisionComment} value={comment} onChange={(e) => setComment(e.target.value)} />
        <button className="rounded bg-purple-700 px-3 py-1 text-sm hover:bg-purple-600" onClick={mark}>
          {t.geometry.markCollision}
        </button>
      </div>
      {notice && <p className="text-sm text-purple-300">{notice}</p>}
    </div>
  );
}

export function MachineCardPage() {
  const machineId = Number(useParams().machineId);
  const { data: me } = useMe();
  const { data: catalog } = useCatalog();
  const { data: machine, isError } = useQuery({ queryKey: ["machine", machineId], queryFn: () => api<Machine>(`/machines/${machineId}`) });
  const status = useQuery({
    queryKey: ["geometry-status", machineId],
    queryFn: () => api<GeometryStatus>(`/machines/${machineId}/ballbar/status`),
    retry: false,
  });
  const tests = useQuery({
    queryKey: ["ballbar-tests", machineId],
    queryFn: () => api<BallbarTest[]>(`/machines/${machineId}/ballbar/tests`),
    retry: false,
  });
  const client = useQueryClient();
  const [selected, setSelected] = useState<number | null>(null);
  const [form, setForm] = useState<{ initial?: BallbarTest } | null>(null);

  if (isError) return <p className="text-slate-400">{t.operator.notFound}</p>;
  if (!machine || !catalog) return <p className="text-slate-400">{t.common.loading}</p>;
  const editable = canEditGeometry(me);
  const noGeometry = status.error instanceof ApiError && status.error.status === 403;
  const live = machine.live;
  const current = selected ?? tests.data?.find((x) => !x.voided_at)?.id ?? null;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-bold">{machine.name}</h1>
        <span className="rounded px-2 py-0.5 text-sm font-semibold text-white" style={{ backgroundColor: stateColor(live?.state) }}>
          {stateLabel(live?.state)}
        </span>
        <span className="text-slate-400">
          {machine.org_name} · {machine.site_name} · {machine.area_name ?? "—"} · {t.geometry.model}: {machine.model ?? "—"} ·{" "}
          {t.geometry.controller}: {machine.controller_type ?? "—"}
        </span>
        <Link className="ml-auto text-sm text-sky-400 hover:underline" to={`/operator/${machine.id}`}>
          {t.operator.title}
        </Link>
      </header>
      {noGeometry ? (
        <p className="text-slate-400">{t.geometry.noAccess}</p>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
            {status.data && <NextCheckCard machineId={machineId} status={status.data} editable={editable} />}
            <div className="space-y-2 rounded-xl bg-slate-900 p-4">
              <h3 className="font-semibold">{t.geometry.latestByPlane}</h3>
              {!status.data?.planes.length && <p className="text-sm text-slate-400">{t.geometry.status.never}</p>}
              {status.data?.planes.map((p) => (
                <div key={p.id} className="text-sm">
                  <button className="font-semibold text-sky-400 hover:underline" onClick={() => setSelected(p.id)}>
                    {p.plane} · {formatDate(p.performed_at)}
                  </button>
                  {p.exceeded?.length ? (
                    p.exceeded.map((r) => (
                      <div key={`${r.parameter}${r.axis}${r.direction}`} className="text-red-300">
                        {exceededText(r)}
                      </div>
                    ))
                  ) : (
                    <span className="ml-2 text-green-400">{t.geometry.status.ok}</span>
                  )}
                </div>
              ))}
            </div>
            <CollisionsCard machineId={machineId} />
          </div>

          <div className="rounded-xl bg-slate-900 p-4">
            <div className="mb-2 flex items-center gap-3">
              <h3 className="font-semibold">{t.geometry.tests}</h3>
              {editable && !form && (
                <button className="rounded bg-sky-600 px-3 py-1 text-sm hover:bg-sky-500" onClick={() => setForm({})}>
                  {t.geometry.newTest}
                </button>
              )}
            </div>
            <table className="w-full text-sm">
              <thead className="text-left text-slate-400">
                <tr>
                  <th className="px-2 py-1">{t.geometry.date}</th>
                  <th className="px-2 py-1">{t.geometry.plane}</th>
                  <th className="px-2 py-1">R, мм</th>
                  <th className="px-2 py-1">F, мм/мин</th>
                  <th className="px-2 py-1">{t.geometry.arc}</th>
                  <th className="px-2 py-1">{t.geometry.engineer}</th>
                  <th className="px-2 py-1">{t.geometry.template}</th>
                  <th className="px-2 py-1">{t.geometry.results}</th>
                  <th className="px-2 py-1">{t.geometry.files}</th>
                </tr>
              </thead>
              <tbody>
                {(tests.data ?? []).map((test) => (
                  <tr
                    key={test.id}
                    onClick={() => setSelected(test.id)}
                    className={`tabular cursor-pointer border-t border-slate-800 hover:bg-slate-800 ${test.id === current ? "bg-slate-800" : ""} ${test.voided_at ? "text-slate-500 line-through" : ""}`}
                  >
                    <td className="px-2 py-1">{new Date(test.performed_at).toLocaleDateString("ru-RU")}</td>
                    <td className="px-2 py-1">{test.plane}</td>
                    <td className="px-2 py-1">{formatNumber(test.radius_mm)}</td>
                    <td className="px-2 py-1">{formatNumber(test.feed_mm_min)}</td>
                    <td className="px-2 py-1">{test.arc_deg}°</td>
                    <td className="px-2 py-1">{test.engineer}</td>
                    <td className="px-2 py-1">{test.template ?? "—"}</td>
                    <td className="px-2 py-1">
                      {test.results_count}
                      {test.exceeded?.length ? <span className="ml-2 rounded bg-red-700 px-1.5 text-white">{test.exceeded.length} {t.geometry.exceeded}</span> : null}
                    </td>
                    <td className="px-2 py-1">{typeof test.files === "number" ? test.files : 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {form && (
            <TestForm
              machineId={machineId}
              catalog={catalog}
              engineer={me?.name ?? ""}
              initial={form.initial}
              onCancel={() => setForm(null)}
              onSaved={async (id) => {
                setForm(null);
                setSelected(id);
                await client.invalidateQueries({ predicate: (q) => String(q.queryKey[0]).startsWith("ballbar") || q.queryKey[0] === "geometry-status" });
              }}
            />
          )}
          {current !== null && !form && <TestDetail key={current} testId={current} catalog={catalog} editable={editable} onEdit={(test) => setForm({ initial: test })} />}
          {status.data && <TrendPanel machineId={machineId} status={status.data} catalog={catalog} />}
          <TolerancePanel catalog={catalog} machineId={machineId} model={machine.model} title={t.geometry.tolerances} />
        </>
      )}
    </div>
  );
}
