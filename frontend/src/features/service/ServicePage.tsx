import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, query, type ArchiveRow, type RepairRequest } from "../../api/client";
import { t } from "../../shared/i18n";
import { formatDate } from "../geometry/common";
import { formatDay, formatMoment, machinePlace, PlanStatusChip, RequestStatusChip } from "../repairs/common";
import { useSchedule } from "../repairs/MaintenancePanel";

const hours = (value: number) => value.toLocaleString("ru-RU", { maximumFractionDigits: 1 });

/** Plans that are due or will be soon: the reminder of maintenance by running hours. */
function MaintenanceReminders() {
  const { data: plans } = useSchedule();
  const urgent = (plans ?? []).filter((p) => p.status !== "ok");
  return (
    <section className="rounded-xl bg-slate-900 p-4">
      <h2 className="mb-2 text-lg font-semibold">{t.maintenance.reminders}</h2>
      {plans && urgent.length === 0 && <p className="text-slate-400">{t.maintenance.noReminders}</p>}
      {urgent.length > 0 && (
        <table className="w-full text-sm">
          <tbody>
            {urgent.map((p) => (
              <tr key={p.id} className="tabular border-t border-slate-800">
                <td className="px-2 py-2">
                  <PlanStatusChip status={p.status} />
                </td>
                <td className="px-2 py-2 font-medium">{p.machine_name}</td>
                <td className="px-2 py-2 text-slate-300">{machinePlace(p)}</td>
                <td className="px-2 py-2">{p.name}</td>
                <td className="px-2 py-2 text-right">
                  {hours(p.run_hours)} / {hours(p.interval_run_hours)} {t.units.h}
                </td>
                <td className="px-2 py-2">{p.status === "due" ? "" : formatDay(p.forecast_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function OpenRequests() {
  const { data: requests } = useQuery({
    queryKey: ["repair-requests", true],
    queryFn: () => api<RepairRequest[]>(`/repair-requests${query({ open: "true" })}`),
    refetchInterval: 30_000,
  });
  return (
    <section className="rounded-xl bg-slate-900 p-4">
      <div className="mb-2 flex items-center gap-3">
        <h2 className="text-lg font-semibold">{t.service.openRequests}</h2>
        <Link className="ml-auto text-sm text-sky-400 hover:underline" to="/repairs">
          {t.service.allRequests}
        </Link>
      </div>
      {requests?.length === 0 && <p className="text-slate-400">{t.service.noOpenRequests}</p>}
      {Boolean(requests?.length) && (
        <table className="w-full text-sm">
          <tbody>
            {(requests ?? []).map((r) => (
              <tr key={r.id} className="tabular border-t border-slate-800">
                <td className="px-2 py-2">
                  <Link className="text-sky-400 hover:underline" to={`/repairs?request=${r.id}`}>
                    №{r.id}
                  </Link>
                </td>
                <td className="px-2 py-2">
                  <RequestStatusChip status={r.status} voided={false} />
                </td>
                <td className="px-2 py-2 font-medium">{r.machine_name}</td>
                <td className="px-2 py-2 text-slate-300">{machinePlace(r)}</td>
                <td className="px-2 py-2">{r.description}</td>
                <td className="whitespace-nowrap px-2 py-2">{formatMoment(r.failure_ts)}</td>
                <td className="px-2 py-2">{r.executor_name ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

/** The service screen: maintenance reminders, open repair requests and the index of the geometry archive. */
export function ServicePage() {
  const { data: rows, isLoading } = useQuery({ queryKey: ["ballbar-archive"], queryFn: () => api<ArchiveRow[]>("/geometry/machines") });
  return (
    <div className="space-y-4">
      <MaintenanceReminders />
      <OpenRequests />
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
