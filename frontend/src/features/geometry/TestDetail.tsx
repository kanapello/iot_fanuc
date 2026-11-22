import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, download, upload, type BallbarFile, type BallbarTest, type Catalog } from "../../api/client";
import { t } from "../../shared/i18n";
import { formatNumber, resultLabel } from "./common";
import { PolarPlot } from "./PolarPlot";

export function TestDetail({
  testId,
  catalog,
  editable,
  onEdit,
}: {
  testId: number;
  catalog: Catalog;
  editable: boolean;
  onEdit: (test: BallbarTest) => void;
}) {
  const client = useQueryClient();
  const { data: test } = useQuery({ queryKey: ["ballbar-test", testId], queryFn: () => api<BallbarTest>(`/ballbar/tests/${testId}`) });
  const [error, setError] = useState("");
  if (!test) return <p className="text-slate-400">{t.common.loading}</p>;
  const files = (Array.isArray(test.files) ? test.files : []) as BallbarFile[];
  const writable = editable && !test.voided_at;
  const refresh = () => client.invalidateQueries({ predicate: (q) => String(q.queryKey[0]).startsWith("ballbar") });

  const onUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setError("");
    try {
      await upload(`/ballbar/tests/${testId}/files`, file);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : t.common.error);
    }
  };
  const onVoid = async () => {
    const reason = window.prompt(t.geometry.voidReason);
    if (!reason) return;
    try {
      await api(`/ballbar/tests/${testId}/void`, { method: "POST", body: { reason } });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : t.common.error);
    }
  };

  return (
    <div className="space-y-4 rounded-xl bg-slate-900 p-4">
      <div className="flex flex-wrap items-baseline gap-3">
        <h3 className={`text-lg font-semibold ${test.voided_at ? "line-through opacity-60" : ""}`}>
          {new Date(test.performed_at).toLocaleString("ru-RU")} · {test.plane} · R{formatNumber(test.radius_mm)} мм · F
          {formatNumber(test.feed_mm_min)} · {test.arc_deg}°
        </h3>
        {test.voided_at && (
          <span className="rounded bg-slate-700 px-2 py-0.5 text-sm">
            {t.geometry.voided}: {test.void_reason}
          </span>
        )}
        {writable && (
          <span className="ml-auto space-x-2">
            <button className="rounded bg-slate-700 px-3 py-1 text-sm hover:bg-slate-600" onClick={() => onEdit(test)}>
              {t.common.edit}
            </button>
            <button className="rounded bg-slate-700 px-3 py-1 text-sm hover:bg-slate-600" onClick={onVoid}>
              {t.geometry.void}
            </button>
          </span>
        )}
      </div>
      <p className="text-sm text-slate-300">
        {t.geometry.engineer}: {test.engineer}
        {test.template ? ` · ${t.geometry.template}: ${test.template}` : ""}
        {test.air_temp_c !== null ? ` · ${t.geometry.airTemp} ${formatNumber(test.air_temp_c)}` : ""}
        {test.machine_temp_c !== null ? ` · ${t.geometry.machineTemp} ${formatNumber(test.machine_temp_c)}` : ""}
        {test.notes ? ` · ${test.notes}` : ""}
      </p>
      {error && <p className="rounded bg-red-900/60 px-3 py-2 text-sm text-red-200">{error}</p>}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <table className="w-full self-start text-sm">
          <thead className="text-left text-slate-400">
            <tr>
              <th className="px-2 py-1">{t.geometry.parameter}</th>
              <th className="px-2 py-1 text-right">{t.geometry.value}</th>
              <th className="px-2 py-1">{t.geometry.unit}</th>
            </tr>
          </thead>
          <tbody>
            {(["iso230_4", "diagnostics"] as const).map((group) => {
              const rows = (test.results ?? []).filter((r) => catalog.parameters.find((p) => p.code === r.parameter)?.group === group);
              if (!rows.length) return null;
              return [
                <tr key={group}>
                  <td colSpan={3} className="px-2 pt-3 font-semibold text-slate-300">
                    {t.geometry.groups[group]}
                  </td>
                </tr>,
                ...rows.map((r) => (
                  <tr key={`${r.parameter}${r.axis}${r.direction}`} className="tabular border-t border-slate-800">
                    <td className="px-2 py-1">{resultLabel(r.parameter, r.axis, r.direction, catalog)}</td>
                    <td className="px-2 py-1 text-right font-semibold">{formatNumber(r.value, 2)}</td>
                    <td className="px-2 py-1 text-slate-400">{r.unit}</td>
                  </tr>
                )),
              ];
            })}
          </tbody>
        </table>
        <div>
          <h4 className="mb-1 font-semibold text-slate-300">{t.geometry.polar}</h4>
          <PolarPlot testId={testId} />
        </div>
      </div>
      <div className="space-y-2">
        <h4 className="font-semibold text-slate-300">{t.geometry.files}</h4>
        {!files.length && <p className="text-sm text-slate-400">{t.geometry.noFiles}</p>}
        {files.map((f) => (
          <div key={f.id} className="flex flex-wrap items-center gap-3 text-sm">
            <button className="text-sky-400 hover:underline" onClick={() => download(`/ballbar/files/${f.id}`, f.filename)}>
              {f.filename}
            </button>
            <span className="text-slate-400">
              {(f.size_bytes / 1024).toFixed(0)} КБ · {f.uploaded_by_name} · {new Date(f.uploaded_at).toLocaleString("ru-RU")}
            </span>
          </div>
        ))}
        {writable && (
          <label className="inline-flex cursor-pointer items-center gap-2 rounded bg-slate-700 px-3 py-2 text-sm hover:bg-slate-600">
            {t.geometry.upload}
            <input type="file" accept=".b5r,.pdf" className="hidden" onChange={onUpload} />
          </label>
        )}
        <p className="text-xs text-slate-500">{t.geometry.keptForever}</p>
      </div>
    </div>
  );
}
