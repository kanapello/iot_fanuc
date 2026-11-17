import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, type Live, type Machine } from "../../api/client";
import { t } from "../../shared/i18n";
import { formatDuration, percent, stateColor, stateLabel } from "../../shared/states";
import { useLive, useNow } from "../../shared/useLive";

function Tile({ machine, live, now }: { machine: Machine; live: Live | undefined; now: number }) {
  const state = live?.state ?? "OFFLINE";
  const since = live?.since ? (now - Date.parse(live.since)) / 1000 : 0;
  const isDown = state !== "RUN";
  return (
    <Link
      to={`/operator/${machine.id}`}
      className="flex flex-col rounded-2xl p-5 text-white shadow-lg"
      style={{ backgroundColor: stateColor(state) }}
    >
      <div className="flex items-baseline justify-between gap-3">
        <span className="truncate text-3xl font-bold xl:text-4xl">{machine.name}</span>
        <span className="tabular text-3xl font-semibold xl:text-4xl">{formatDuration(since)}</span>
      </div>
      <div className="mt-1 text-4xl font-extrabold uppercase tracking-wide xl:text-5xl">{stateLabel(state)}</div>
      <div className="mt-2 min-h-[2rem] text-xl font-medium xl:text-2xl">
        {isDown && state !== "IDLE" ? (live?.reason?.name ?? t.dashboard.noReason) : ""}
      </div>
      <div className="mt-auto grid grid-cols-3 gap-2 pt-3 text-black/80">
        <div>
          <div className="text-sm font-semibold uppercase">{t.dashboard.program}</div>
          <div className="text-2xl font-bold">{live?.program ?? "—"}</div>
        </div>
        <div>
          <div className="text-sm font-semibold uppercase">{t.dashboard.parts}</div>
          <div className="tabular text-2xl font-bold">
            {live?.shift ? `${live.shift.good_parts}${live.shift.plan_now ? ` / ${live.shift.plan_now}` : ""}` : "—"}
          </div>
        </div>
        <div className="text-right">
          <div className="text-sm font-semibold uppercase">{t.dashboard.oeeShift}</div>
          <div className="tabular text-2xl font-bold">{percent(live?.shift?.oee)}</div>
        </div>
      </div>
    </Link>
  );
}

export function DashboardPage() {
  const { areaId } = useParams();
  const { data: machines } = useQuery({ queryKey: ["machines"], queryFn: () => api<Machine[]>("/machines") });
  const { machines: live, connected } = useLive();
  const now = useNow();

  const visible = (machines ?? []).filter((m) => !areaId || String(m.area_id) === areaId);
  const areas = [...new Map(visible.map((m) => [m.area_id, m.area_name ?? ""])).entries()];

  return (
    <div className="min-h-full p-5">
      {!connected && (
        <div className="mb-4 rounded-lg bg-amber-500 px-4 py-2 text-lg font-semibold text-black">
          {t.common.connectionLost}
        </div>
      )}
      {!machines && <p className="text-2xl text-slate-400">{t.common.loading}</p>}
      {areas.map(([id, name]) => (
        <section key={String(id)} className="mb-6">
          <h2 className="mb-3 text-2xl font-semibold text-slate-300">{name}</h2>
          <div className="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-3">
            {visible
              .filter((m) => m.area_id === id)
              .map((m) => (
                <Tile key={m.id} machine={m} live={live[m.id] ?? m.live ?? undefined} now={now} />
              ))}
          </div>
        </section>
      ))}
    </div>
  );
}
