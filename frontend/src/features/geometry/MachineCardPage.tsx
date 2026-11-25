import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type BallbarTest, type CollisionMark, type Machine, type Repair } from "../../api/client";
import { t } from "../../shared/i18n";
import { useMe } from "../../shared/session";
import { stateColor, stateLabel } from "../../shared/states";
import { canUseArchive, daysSince, formatDate, formatNumber, useCatalog } from "./common";
import { TestDetail } from "./TestDetail";
import { TestForm } from "./TestForm";
import { TrendPanel } from "./TrendPanel";

const input = "rounded border border-slate-600 bg-slate-800 px-2 py-1 text-sm";
const refreshArchive = (client: ReturnType<typeof useQueryClient>) =>
  client.invalidateQueries({ predicate: (q) => String(q.queryKey[0]).startsWith("ballbar") });

function LastTestCard({ tests }: { tests: BallbarTest[] | undefined }) {
  const last = tests?.find((test) => !test.voided_at);
  return (
    <div className="space-y-1 rounded-xl bg-slate-900 p-4">
      <h3 className="font-semibold">{t.geometry.lastTest}</h3>
      {last ? (
        <>
          <div className="text-2xl font-bold">{formatDate(last.performed_at)}</div>
          <div className="text-slate-300">
            {t.geometry.daysAgo}: <span className="tabular font-semibold">{daysSince(last.performed_at)}</span>
          </div>
        </>
      ) : (
        <p className="text-slate-400">{t.geometry.noTests}</p>
      )}
    </div>
  );
}

/** A voided mark or repair stays in the list, struck through, with the reason; it leaves the trend. */
function VoidableRow({
  voidedAt,
  voidReason,
  prompt,
  path,
  onError,
  children,
}: {
  voidedAt: string | null;
  voidReason: string | null;
  prompt: string;
  path: string;
  onError: (message: string) => void;
  children: React.ReactNode;
}) {
  const client = useQueryClient();
  const onVoid = async () => {
    const reason = window.prompt(prompt);
    if (!reason) return;
    try {
      await api(path, { method: "POST", body: { reason } });
      await refreshArchive(client);
    } catch (e) {
      onError(e instanceof Error ? e.message : t.common.error);
    }
  };
  return (
    <div className="flex items-start gap-2 text-sm">
      <div className={`flex-1 ${voidedAt ? "text-slate-500" : ""}`}>
        <span className={voidedAt ? "line-through" : ""}>{children}</span>
        {voidedAt && (
          <span className="ml-2 rounded bg-slate-700 px-1.5 py-0.5 text-xs text-slate-300">
            {t.geometry.voidedRecord}: {voidReason}
          </span>
        )}
      </div>
      {!voidedAt && (
        <button className="text-xs text-slate-400 hover:text-red-300 hover:underline" onClick={onVoid}>
          {t.geometry.void}
        </button>
      )}
    </div>
  );
}

function CollisionsCard({ machineId }: { machineId: number }) {
  const client = useQueryClient();
  const { data: marks } = useQuery({
    queryKey: ["ballbar-collisions", machineId],
    queryFn: () => api<CollisionMark[]>(`/machines/${machineId}/collisions`),
  });
  const [comment, setComment] = useState("");
  const [notice, setNotice] = useState("");
  const mark = async () => {
    if (!window.confirm(t.geometry.confirmCollision)) return;
    try {
      await api(`/machines/${machineId}/collisions`, { method: "POST", body: { comment: comment || null } });
      setComment("");
      setNotice(t.geometry.collisionSaved);
      await refreshArchive(client);
    } catch (e) {
      setNotice(e instanceof Error ? e.message : t.common.error);
    }
  };
  return (
    <div className="space-y-2 rounded-xl bg-slate-900 p-4">
      <h3 className="font-semibold">{t.geometry.collisions}</h3>
      {!marks?.length && <p className="text-sm text-slate-400">{t.common.noData}</p>}
      {(marks ?? []).slice(0, 5).map((m) => (
        <VoidableRow key={m.id} voidedAt={m.voided_at} voidReason={m.void_reason} prompt={t.geometry.voidMarkReason} path={`/collisions/${m.id}/void`} onError={setNotice}>
          {new Date(m.ts).toLocaleString("ru-RU")} · {m.comment ?? "—"} {m.operator ? `(${m.operator})` : ""}
        </VoidableRow>
      ))}
      <div className="flex flex-wrap gap-2">
        <input className={`${input} min-w-0 flex-1`} placeholder={t.geometry.collisionComment} value={comment} onChange={(e) => setComment(e.target.value)} />
        <button className="rounded bg-purple-700 px-3 py-1 text-sm hover:bg-purple-600" onClick={mark}>
          {t.geometry.markCollision}
        </button>
      </div>
      {notice && <p className="text-sm text-purple-300">{notice}</p>}
    </div>
  );
}

function localNow() {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

function RepairsCard({ machineId }: { machineId: number }) {
  const client = useQueryClient();
  const { data: repairs } = useQuery({
    queryKey: ["ballbar-repairs", machineId],
    queryFn: () => api<Repair[]>(`/machines/${machineId}/repairs`),
  });
  const [date, setDate] = useState(localNow());
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");
  const add = async () => {
    setError("");
    try {
      await api(`/machines/${machineId}/repairs`, {
        method: "POST",
        body: { completed_at: new Date(`${date}T12:00:00`).toISOString(), description },
      });
      setDescription("");
      await refreshArchive(client);
    } catch (e) {
      setError(e instanceof Error ? e.message : t.common.error);
    }
  };
  return (
    <div className="space-y-2 rounded-xl bg-slate-900 p-4">
      <h3 className="font-semibold">{t.geometry.repairs}</h3>
      {!repairs?.length && <p className="text-sm text-slate-400">{t.common.noData}</p>}
      {(repairs ?? []).slice(0, 5).map((r) => (
        <VoidableRow key={r.id} voidedAt={r.voided_at} voidReason={r.void_reason} prompt={t.geometry.voidRepairReason} path={`/repairs/${r.id}/void`} onError={setError}>
          {formatDate(r.completed_at)} · {r.description} {r.engineer ? `(${r.engineer})` : ""}
        </VoidableRow>
      ))}
      {error && <p className="text-sm text-red-300">{error}</p>}
      <div className="flex flex-wrap gap-2">
        <input type="date" className={input} value={date} onChange={(e) => setDate(e.target.value)} />
        <input className={`${input} min-w-0 flex-1`} placeholder={t.geometry.repairDescription} value={description} onChange={(e) => setDescription(e.target.value)} />
        <button className="rounded bg-green-700 px-3 py-1 text-sm hover:bg-green-600" onClick={add}>
          {t.geometry.addRepair}
        </button>
      </div>
    </div>
  );
}

export function MachineCardPage() {
  const machineId = Number(useParams().machineId);
  const { data: me } = useMe();
  const archive = canUseArchive(me);
  const { data: catalog } = useCatalog(archive);
  const { data: machine, isError } = useQuery({ queryKey: ["machine", machineId], queryFn: () => api<Machine>(`/machines/${machineId}`) });
  const tests = useQuery({
    queryKey: ["ballbar-tests", machineId],
    queryFn: () => api<BallbarTest[]>(`/machines/${machineId}/ballbar/tests`),
    enabled: archive,
  });
  const client = useQueryClient();
  const [selected, setSelected] = useState<number | null>(null);
  const [form, setForm] = useState<{ initial?: BallbarTest } | null>(null);

  if (me && !archive) return <p className="text-slate-400">{t.common.noAccess}</p>;
  if (isError) return <p className="text-slate-400">{t.operator.notFound}</p>;
  if (!machine || !catalog) return <p className="text-slate-400">{t.common.loading}</p>;
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
          {machine.org_name} · {machine.site_name} · {machine.area_name ?? "—"} · {t.geometry.controller}: {machine.controller_type ?? "—"}
        </span>
        <Link className="ml-auto text-sm text-sky-400 hover:underline" to={`/operator/${machine.id}`}>
          {t.operator.title}
        </Link>
      </header>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <LastTestCard tests={tests.data} />
        <CollisionsCard machineId={machineId} />
        <RepairsCard machineId={machineId} />
      </div>

      <div className="rounded-xl bg-slate-900 p-4">
        <div className="mb-2 flex items-center gap-3">
          <h3 className="font-semibold">{t.geometry.tests}</h3>
          {!form && (
            <button className="rounded bg-sky-600 px-3 py-1 text-sm hover:bg-sky-500" onClick={() => setForm({})}>
              {t.geometry.newTest}
            </button>
          )}
        </div>
        <div className="overflow-x-auto">
        <table className="w-full whitespace-nowrap text-sm">
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
                <td className="px-2 py-1">{test.results_count}</td>
                <td className="px-2 py-1">{typeof test.files === "number" ? test.files : 0}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
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
            await refreshArchive(client);
          }}
        />
      )}
      {current !== null && !form && <TestDetail key={current} testId={current} catalog={catalog} editable onEdit={(test) => setForm({ initial: test })} />}
      <TrendPanel machineId={machineId} catalog={catalog} />
    </div>
  );
}

