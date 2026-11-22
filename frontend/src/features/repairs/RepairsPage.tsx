import { useSearchParams } from "react-router-dom";
import { t } from "../../shared/i18n";
import { useMe } from "../../shared/session";
import { MaintenancePanel } from "./MaintenancePanel";
import { RequestsPanel } from "./RequestsPanel";

/** Repair requests and the maintenance schedule: the customer's view of their machines, the service's desk. */
export function RepairsPage() {
  const { data: me } = useMe();
  const [params] = useSearchParams();
  const focus = Number(params.get("request")) || null;
  if (!me) return <p className="text-slate-400">{t.common.loading}</p>;
  if (!me.menu.repairs) return <p className="text-slate-400">{t.common.noAccess}</p>;
  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">{t.repairs.title}</h1>
      <RequestsPanel me={me} focus={focus} />
      <MaintenancePanel me={me} />
    </div>
  );
}
