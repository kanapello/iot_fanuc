import { useState } from "react";
import { api, type BallbarTest, type Catalog } from "../../api/client";
import { t } from "../../shared/i18n";
import { keysOf, resultKey, resultLabel } from "./common";

const input = "w-full rounded border border-slate-600 bg-slate-800 px-2 py-1";

function localInput(iso?: string) {
  const date = iso ? new Date(iso) : new Date();
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

const parse = (value: string) => (value.trim() === "" ? null : Number(value.replace(",", ".")));

/** Manual entry of a ballbar test from the Ballbar 20 report: set-up and results. */
export function TestForm({
  machineId,
  catalog,
  engineer,
  initial,
  onSaved,
  onCancel,
}: {
  machineId: number;
  catalog: Catalog;
  engineer: string;
  initial?: BallbarTest;
  onSaved: (id: number) => void;
  onCancel: () => void;
}) {
  const [setup, setSetup] = useState({
    performed_at: localInput(initial?.performed_at),
    engineer: initial?.engineer ?? engineer,
    plane: initial?.plane ?? "XY",
    radius_mm: String(initial?.radius_mm ?? 100),
    feed_mm_min: String(initial?.feed_mm_min ?? 1000),
    arc_deg: String(initial?.arc_deg ?? 360),
    air_temp_c: initial?.air_temp_c?.toString() ?? "",
    machine_temp_c: initial?.machine_temp_c?.toString() ?? "",
    template: initial?.template ?? "",
    notes: initial?.notes ?? "",
  });
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries((initial?.results ?? []).map((r) => [resultKey(r.parameter, r.axis, r.direction), String(r.value)])),
  );
  const [error, setError] = useState("");
  const set = (key: keyof typeof setup) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) =>
    setSetup((s) => ({ ...s, [key]: e.target.value }));

  const save = async () => {
    setError("");
    const results = catalog.parameters.flatMap((p) =>
      keysOf(p, setup.plane, catalog)
        .map(([axis, direction]) => ({ parameter: p.code, axis, direction, value: parse(values[resultKey(p.code, axis, direction)] ?? "") }))
        .filter((r) => r.value !== null),
    );
    if (results.some((r) => Number.isNaN(r.value))) {
      setError(t.common.error);
      return;
    }
    const body = {
      performed_at: new Date(setup.performed_at).toISOString(),
      engineer: setup.engineer,
      plane: setup.plane,
      radius_mm: parse(setup.radius_mm),
      feed_mm_min: parse(setup.feed_mm_min),
      arc_deg: Number(setup.arc_deg),
      air_temp_c: parse(setup.air_temp_c),
      machine_temp_c: parse(setup.machine_temp_c),
      template: setup.template || null,
      notes: setup.notes || null,
      results,
    };
    try {
      if (initial) {
        await api(`/ballbar/tests/${initial.id}`, { method: "PUT", body });
        onSaved(initial.id);
      } else {
        const created = await api<{ id: number }>(`/machines/${machineId}/ballbar/tests`, { method: "POST", body });
        onSaved(created.id);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : t.common.error);
    }
  };

  const field = (label: string, control: React.ReactNode) => (
    <label className="block space-y-1 text-sm">
      <span className="text-slate-400">{label}</span>
      {control}
    </label>
  );
  const groups = ["iso230_4", "diagnostics"] as const;
  return (
    <div className="space-y-4 rounded-xl bg-slate-900 p-4">
      <h3 className="text-lg font-semibold">{initial ? t.geometry.editTest : t.geometry.newTest}</h3>
      {error && <p className="rounded bg-red-900/60 px-3 py-2 text-sm text-red-200">{error}</p>}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-5">
        {field(t.geometry.date, <input type="datetime-local" className={input} value={setup.performed_at} onChange={set("performed_at")} />)}
        {field(t.geometry.engineer, <input className={input} value={setup.engineer} onChange={set("engineer")} />)}
        {field(
          t.geometry.plane,
          <select className={input} value={setup.plane} onChange={set("plane")}>
            {catalog.planes.map((p) => (
              <option key={p}>{p}</option>
            ))}
          </select>,
        )}
        {field(t.geometry.radius, <input className={input} inputMode="decimal" value={setup.radius_mm} onChange={set("radius_mm")} />)}
        {field(t.geometry.feed, <input className={input} inputMode="decimal" value={setup.feed_mm_min} onChange={set("feed_mm_min")} />)}
        {field(
          t.geometry.arc,
          <select className={input} value={setup.arc_deg} onChange={set("arc_deg")}>
            {catalog.arcs.map((a) => (
              <option key={a} value={a}>
                {a}°
              </option>
            ))}
          </select>,
        )}
        {field(t.geometry.airTemp, <input className={input} inputMode="decimal" value={setup.air_temp_c} onChange={set("air_temp_c")} />)}
        {field(t.geometry.machineTemp, <input className={input} inputMode="decimal" value={setup.machine_temp_c} onChange={set("machine_temp_c")} />)}
        {field(t.geometry.template, <input className={input} value={setup.template} onChange={set("template")} />)}
        {field(t.geometry.notes, <input className={input} value={setup.notes} onChange={set("notes")} />)}
      </div>
      <p className="text-sm text-slate-400">{t.geometry.resultsHint}</p>
      {groups.map((group) => (
        <div key={group}>
          <h4 className="mb-2 font-semibold text-slate-300">{t.geometry.groups[group]}</h4>
          <div className="grid grid-cols-1 gap-x-6 gap-y-2 md:grid-cols-2">
            {catalog.parameters
              .filter((p) => p.group === group)
              .map((p) => (
                <div key={p.code} className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="w-48 shrink-0">
                    {t.geometry.params[p.code]} <span className="text-slate-500">{p.unit}</span>
                  </span>
                  {keysOf(p, setup.plane, catalog).map(([axis, direction]) => {
                    const key = resultKey(p.code, axis, direction);
                    const caption = resultLabel(p.code, axis, direction, catalog).replace(t.geometry.params[p.code] ?? "", "").trim();
                    return (
                      <label key={key} className="flex items-center gap-1">
                        {caption && <span className="text-xs text-slate-400">{caption.replace(/[()]/g, "")}</span>}
                        <input
                          className="w-20 rounded border border-slate-600 bg-slate-800 px-2 py-1 text-right"
                          inputMode="decimal"
                          value={values[key] ?? ""}
                          onChange={(e) => setValues((v) => ({ ...v, [key]: e.target.value }))}
                        />
                      </label>
                    );
                  })}
                </div>
              ))}
          </div>
        </div>
      ))}
      <div className="flex gap-2">
        <button className="rounded bg-sky-600 px-4 py-2 font-medium hover:bg-sky-500" onClick={save}>
          {t.common.save}
        </button>
        <button className="rounded bg-slate-700 px-4 py-2 hover:bg-slate-600" onClick={onCancel}>
          {t.common.cancel}
        </button>
      </div>
    </div>
  );
}
