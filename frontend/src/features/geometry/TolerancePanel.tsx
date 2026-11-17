import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, query, type Catalog, type ToleranceRow } from "../../api/client";
import { t } from "../../shared/i18n";
import { formatNumber, resultLabel } from "./common";

const input = "rounded border border-slate-600 bg-slate-800 px-2 py-1 text-sm";

/** Tolerances of one machine (with the rows of its model), or of one model. */
export function TolerancePanel({
  catalog,
  machineId,
  model,
  title,
}: {
  catalog: Catalog;
  machineId?: number;
  model: string | null;
  title: string;
}) {
  const client = useQueryClient();
  const path = `/geometry/tolerances${query({ model: machineId ? undefined : model })}`;
  const { data } = useQuery({
    queryKey: ["ballbar-tolerances", path],
    queryFn: () => api<{ rows: ToleranceRow[]; models: string[]; models_editable: boolean }>(path),
  });
  const [draft, setDraft] = useState({ parameter: "squareness", plane: "", axis: "", limit_value: "" });
  const [error, setError] = useState("");
  const rows = (data?.rows ?? []).filter((r) =>
    machineId ? r.machine_id === machineId || (model !== null && r.machine_model === model) : r.machine_model === model,
  );
  const canAdd = machineId ? true : Boolean(data?.models_editable);
  const refresh = () => client.invalidateQueries({ predicate: (q) => String(q.queryKey[0]).startsWith("ballbar") || q.queryKey[0] === "service-worklist" });

  const run = async (action: () => Promise<unknown>) => {
    setError("");
    try {
      await action();
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : t.common.error);
    }
  };
  const parameter = catalog.parameters.find((p) => p.code === draft.parameter);
  const add = () =>
    run(() =>
      api("/geometry/tolerances", {
        method: "POST",
        body: {
          ...(machineId ? { machine_id: machineId } : { machine_model: model }),
          parameter: draft.parameter,
          plane: draft.plane || null,
          axis: parameter?.per_axis ? draft.axis || null : null,
          limit_value: Number(draft.limit_value.replace(",", ".")),
        },
      }),
    );
  const change = (row: ToleranceRow) => {
    const value = window.prompt(t.geometry.limit, String(row.limit_value));
    if (!value) return;
    run(() =>
      api(`/geometry/tolerances/${row.id}`, {
        method: "PUT",
        body: { parameter: row.parameter, plane: row.plane, axis: row.axis, limit_value: Number(value.replace(",", ".")) },
      }),
    );
  };

  return (
    <div className="space-y-2 rounded-xl bg-slate-900 p-4">
      <h3 className="font-semibold">{title}</h3>
      {error && <p className="rounded bg-red-900/60 px-3 py-2 text-sm text-red-200">{error}</p>}
      <table className="w-full text-sm">
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-t border-slate-800">
              <td className="px-2 py-1">{resultLabel(r.parameter, r.axis, null)}</td>
              <td className="px-2 py-1 text-slate-400">{r.plane ?? t.geometry.anyPlane}</td>
              <td className="tabular px-2 py-1 text-right">± {formatNumber(r.limit_value, 2)} {r.unit}</td>
              <td className="px-2 py-1 text-slate-400">{r.machine_id ? t.geometry.source.machine : `${t.geometry.source.model} ${r.machine_model}`}</td>
              <td className="whitespace-nowrap px-2 py-1 text-right">
                {r.editable && (
                  <span className="space-x-2">
                    <button className="text-sky-400 hover:underline" onClick={() => change(r)}>
                      {t.common.edit}
                    </button>
                    <button className="text-red-300 hover:underline" onClick={() => window.confirm(t.admin.confirmDelete) && run(() => api(`/geometry/tolerances/${r.id}`, { method: "DELETE" }))}>
                      {t.common.delete}
                    </button>
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {canAdd && (model || machineId) && (
        <div className="flex flex-wrap items-center gap-2">
          <select className={input} value={draft.parameter} onChange={(e) => setDraft({ ...draft, parameter: e.target.value, axis: "" })}>
            {catalog.parameters.map((p) => (
              <option key={p.code} value={p.code}>
                {t.geometry.params[p.code]} ({p.unit})
              </option>
            ))}
          </select>
          <select className={input} value={draft.plane} onChange={(e) => setDraft({ ...draft, plane: e.target.value, axis: "" })}>
            <option value="">{t.geometry.anyPlane}</option>
            {catalog.planes.map((p) => (
              <option key={p}>{p}</option>
            ))}
          </select>
          {parameter?.per_axis && (
            <select className={input} value={draft.axis} onChange={(e) => setDraft({ ...draft, axis: e.target.value })}>
              <option value="">{t.geometry.anyAxis}</option>
              {(draft.plane ? catalog.plane_axes[draft.plane] : ["X", "Y", "Z"]).map((a) => (
                <option key={a}>{a}</option>
              ))}
            </select>
          )}
          <input className={`${input} w-24`} placeholder={t.geometry.limit} inputMode="decimal" value={draft.limit_value} onChange={(e) => setDraft({ ...draft, limit_value: e.target.value })} />
          <button className="rounded bg-sky-600 px-3 py-1 text-sm hover:bg-sky-500" onClick={add}>
            {machineId ? t.geometry.addTolerance : t.geometry.addModelTolerance}
          </button>
        </div>
      )}
    </div>
  );
}
