import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type WorkItem } from "../../api/client";
import { t } from "../../shared/i18n";
import { exceededText, formatDate, formatNumber, shortTriggers, useCatalog } from "../geometry/common";
import { TolerancePanel } from "../geometry/TolerancePanel";

const SEVERITY_COLORS: Record<string, string> = { high: "#ef4444", medium: "#eab308", low: "#64748b" };

function What({ item }: { item: WorkItem }) {
  if (item.kind === "tolerance") {
    return (
      <div>
        {item.plane}:{" "}
        {(item.details ?? []).map((d) => (
          <span key={`${d.parameter}${d.axis}${d.direction}`} className="mr-3 text-red-300">
            {exceededText(d)}
          </span>
        ))}
      </div>
    );
  }
  const check = item.next_check;
  if (!check) return null;
  if (item.kind === "check_event") {
    const { shown, more } = shortTriggers(check.triggers);
    return (
      <div>
        {shown.map((tr, i) => (
          <div key={i} className="text-purple-300">
            {t.geometry.trigger[tr.kind]} {tr.code ?? ""} {tr.message ?? ""} · {new Date(tr.ts).toLocaleString("ru-RU")}
          </div>
        ))}
        {more > 0 && <div className="text-purple-300">{t.geometry.moreTriggers} {more}</div>}
      </div>
    );
  }
  return (
    <div className="text-slate-300">
      {check.last_test_at ? `${t.geometry.lastTest}: ${formatDate(check.last_test_at)}` : ""}
      {check.due_date ? ` · ${t.geometry.dueDate}: ${formatDate(check.due_date)}` : ""}
      {check.run_hours_left !== null ? ` · ${t.geometry.hoursLeft}: ${formatNumber(check.run_hours_left)}` : ""}
    </div>
  );
}

export function ServicePage() {
  const { data: items, isLoading } = useQuery({
    queryKey: ["service-worklist"],
    queryFn: () => api<WorkItem[]>("/service/worklist"),
    refetchInterval: 60_000,
  });
  const { data: catalog } = useCatalog();
  const [kind, setKind] = useState<string>("");
  const { data: tolerances } = useQuery({
    queryKey: ["ballbar-tolerances", "models"],
    queryFn: () => api<{ models: string[] }>("/geometry/tolerances"),
  });
  const [model, setModel] = useState<string>("");
  const kinds = Object.keys(t.service.kinds);
  const shown = (items ?? []).filter((i) => !kind || i.kind === kind);
  const chip = (value: string, label: string, count: number) => (
    <button
      key={value}
      onClick={() => setKind(value)}
      className={`rounded px-3 py-1 text-sm ${kind === value ? "bg-sky-600" : "bg-slate-800 hover:bg-slate-700"}`}
    >
      {label} ({count})
    </button>
  );
  const currentModel = model || tolerances?.models[0] || "";

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">{t.service.worklist}</h1>
      <div className="flex flex-wrap gap-2">
        {chip("", t.service.filterAll, items?.length ?? 0)}
        {kinds.map((k) => chip(k, t.service.kinds[k], (items ?? []).filter((i) => i.kind === k).length))}
      </div>
      {isLoading && <p className="text-slate-400">{t.common.loading}</p>}
      {items && !items.some((i) => i.severity !== "low") && <p className="text-green-400">{t.service.empty}</p>}
      <div className="rounded-xl bg-slate-900 p-4">
        <table className="w-full text-sm">
          <thead className="text-left text-slate-400">
            <tr>
              <th className="px-2 py-1" />
              <th className="px-2 py-1">{t.service.machine}</th>
              <th className="px-2 py-1">{t.service.where}</th>
              <th className="px-2 py-1">{t.service.what}</th>
              <th className="px-2 py-1">{t.service.when}</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((item, i) => (
              <tr key={i} className="border-t border-slate-800 align-top">
                <td className="px-2 py-2">
                  <span className="rounded px-2 py-0.5 text-xs font-semibold text-white" style={{ backgroundColor: SEVERITY_COLORS[item.severity] }}>
                    {t.service.severity[item.severity]}
                  </span>
                </td>
                <td className="px-2 py-2">
                  <Link className="font-semibold text-sky-400 hover:underline" to={`/machines/${item.machine_id}`}>
                    {item.machine_name}
                  </Link>
                  <div className="text-xs text-slate-400">{item.machine_model ?? ""}</div>
                </td>
                <td className="px-2 py-2 text-slate-300">
                  {item.org_name} · {item.site_name} · {item.area_name ?? "—"}
                </td>
                <td className="px-2 py-2">
                  <div className="font-medium">{t.service.kinds[item.kind]}</div>
                  <What item={item} />
                </td>
                <td className="whitespace-nowrap px-2 py-2">{formatDate(item.at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {catalog && (tolerances?.models.length ?? 0) > 0 && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-semibold">{t.geometry.modelTolerances}</h2>
            <select className="rounded border border-slate-700 bg-slate-800 px-2 py-1 text-sm" value={currentModel} onChange={(e) => setModel(e.target.value)}>
              {tolerances?.models.map((m) => (
                <option key={m}>{m}</option>
              ))}
            </select>
          </div>
          <TolerancePanel key={currentModel} catalog={catalog} model={currentModel} title={`${t.geometry.model} ${currentModel}`} />
        </div>
      )}
    </div>
  );
}
