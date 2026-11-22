import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type ArchiveRow } from "../../api/client";
import { t } from "../../shared/i18n";
import { formatDate } from "../geometry/common";

/** Index of the geometry archive: when each machine was last tested. No statuses, no verdicts. */
export function ServicePage() {
  const { data: rows, isLoading } = useQuery({ queryKey: ["ballbar-archive"], queryFn: () => api<ArchiveRow[]>("/geometry/machines") });
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">{t.service.archive}</h1>
      {isLoading && <p className="text-slate-400">{t.common.loading}</p>}
      <div className="rounded-xl bg-slate-900 p-4">
        <table className="w-full text-sm">
          <thead className="text-left text-slate-400">
            <tr>
              <th className="px-2 py-1">{t.service.machine}</th>
              <th className="px-2 py-1">{t.service.where}</th>
              <th className="px-2 py-1">{t.geometry.lastTest}</th>
              <th className="px-2 py-1 text-right">{t.geometry.daysAgo}</th>
              <th className="px-2 py-1 text-right">{t.geometry.tests}</th>
            </tr>
          </thead>
          <tbody>
            {(rows ?? []).map((row) => (
              <tr key={row.machine_id} className="tabular border-t border-slate-800">
                <td className="px-2 py-2">
                  <Link className="font-semibold text-sky-400 hover:underline" to={`/machines/${row.machine_id}`}>
                    {row.machine_name}
                  </Link>
                </td>
                <td className="px-2 py-2 text-slate-300">
                  {row.org_name} · {row.site_name} · {row.area_name ?? "—"}
                </td>
                <td className="px-2 py-2">{row.last_test_at ? formatDate(row.last_test_at) : t.geometry.noTests}</td>
                <td className="px-2 py-2 text-right">{row.days_since ?? "—"}</td>
                <td className="px-2 py-2 text-right">{row.tests}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
