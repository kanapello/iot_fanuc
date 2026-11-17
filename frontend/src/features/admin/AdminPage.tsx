import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError } from "../../api/client";
import { t } from "../../shared/i18n";

type Row = Record<string, any>;
type ColumnType = "text" | "number" | "bool" | "select" | "json" | "readonly";
interface Column {
  key: string;
  label: string;
  type: ColumnType;
  options?: { value: string | number | null; label: string }[];
}
interface Tab {
  key: string;
  label: string;
  resource: string;
  columns: Column[];
  canCreate?: boolean;
  canDelete?: boolean;
  blank: Row;
}

const c = t.admin.col;

function useTabs(areas: Row[], reasons: Row[], machines: Row[]): Tab[] {
  const areaOptions = [{ value: null, label: "—" }, ...areas.map((a) => ({ value: a.id, label: a.name }))];
  const groupOptions = [
    { value: null, label: "— (группа)" },
    ...reasons.filter((r) => r.parent_id === null).map((r) => ({ value: r.id, label: r.name })),
  ];
  const machineOptions = [{ value: null, label: "—" }, ...machines.map((m) => ({ value: m.id, label: m.name }))];
  const categories = Object.entries(t.admin.category).map(([value, label]) => ({ value, label }));
  return [
    {
      key: "machines",
      label: t.admin.machines,
      resource: "machines",
      canCreate: false,
      canDelete: false,
      blank: {},
      columns: [
        { key: "id", label: c.id, type: "readonly" },
        { key: "name", label: c.name, type: "text" },
        { key: "area_id", label: c.area, type: "select", options: areaOptions },
        { key: "controller_type", label: c.controller, type: "text" },
        { key: "sort_order", label: c.order, type: "number" },
        { key: "is_active", label: c.active, type: "bool" },
      ],
    },
    {
      key: "areas",
      label: t.admin.areas,
      resource: "areas",
      blank: { site_id: "plant1", name: "" },
      columns: [
        { key: "id", label: c.id, type: "readonly" },
        { key: "name", label: c.name, type: "text" },
      ],
    },
    {
      key: "shifts",
      label: t.admin.shifts,
      resource: "shift-templates",
      blank: { site_id: "plant1", name: "", start_time: "08:00:00", end_time: "16:00:00", days_of_week: [1, 2, 3, 4, 5, 6, 7], planned_breaks: [], is_active: true },
      columns: [
        { key: "name", label: c.name, type: "text" },
        { key: "start_time", label: c.start, type: "text" },
        { key: "end_time", label: c.end, type: "text" },
        { key: "days_of_week", label: c.days, type: "json" },
        { key: "planned_breaks", label: c.breaks, type: "json" },
        { key: "is_active", label: c.active, type: "bool" },
      ],
    },
    {
      key: "reasons",
      label: t.admin.reasons,
      resource: "downtime-reasons",
      blank: { parent_id: null, code: "", name: "", category: "unplanned", color: "#64748b", requires_comment: false, sort_order: 0, is_active: true },
      columns: [
        { key: "parent_id", label: c.parent, type: "select", options: groupOptions },
        { key: "code", label: c.code, type: "text" },
        { key: "name", label: c.name, type: "text" },
        { key: "category", label: c.category, type: "select", options: categories },
        { key: "color", label: c.color, type: "text" },
        { key: "sort_order", label: c.order, type: "number" },
        { key: "is_active", label: c.active, type: "bool" },
      ],
    },
    {
      key: "scrapReasons",
      label: t.admin.scrapReasons,
      resource: "scrap-reasons",
      blank: { code: "", name: "", is_active: true },
      columns: [
        { key: "code", label: c.code, type: "text" },
        { key: "name", label: c.name, type: "text" },
        { key: "is_active", label: c.active, type: "bool" },
      ],
    },
    {
      key: "programs",
      label: t.admin.programs,
      resource: "programs",
      blank: { machine_id: null, program: "", ideal_cycle_time_s: 60, parts_per_cycle: 1 },
      columns: [
        { key: "program", label: c.program, type: "text" },
        { key: "machine_id", label: c.machine, type: "select", options: machineOptions },
        { key: "ideal_cycle_time_s", label: c.cycle, type: "number" },
        { key: "parts_per_cycle", label: c.partsPerCycle, type: "number" },
        { key: "valid_from", label: c.validFrom, type: "readonly" },
      ],
    },
  ];
}

const inputClass = "w-full rounded border border-slate-600 bg-slate-800 px-2 py-1";

function Cell({ column, value, editing, onChange }: { column: Column; value: any; editing: boolean; onChange: (v: any) => void }) {
  if (!editing || column.type === "readonly") {
    if (column.type === "bool") return <>{value ? t.common.yes : t.common.no}</>;
    if (column.type === "select") return <>{column.options?.find((o) => o.value === value)?.label ?? value ?? "—"}</>;
    if (column.type === "json") return <code className="text-xs">{JSON.stringify(value)}</code>;
    if (column.key === "color") {
      return (
        <span className="flex items-center gap-2">
          <span className="inline-block h-4 w-4 rounded" style={{ backgroundColor: value }} />
          {value}
        </span>
      );
    }
    return <>{value ?? "—"}</>;
  }
  if (column.type === "bool") return <input type="checkbox" className="h-5 w-5" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />;
  if (column.type === "select") {
    return (
      <select
        className={inputClass}
        value={value === null || value === undefined ? "" : String(value)}
        onChange={(e) => {
          const option = column.options?.find((o) => String(o.value ?? "") === e.target.value);
          onChange(option ? option.value : null);
        }}
      >
        {column.options?.map((o) => (
          <option key={String(o.value)} value={o.value === null ? "" : String(o.value)}>
            {o.label}
          </option>
        ))}
      </select>
    );
  }
  if (column.type === "json") {
    return <input className={`${inputClass} font-mono text-xs`} value={typeof value === "string" ? value : JSON.stringify(value)} onChange={(e) => onChange(e.target.value)} />;
  }
  return (
    <input
      className={inputClass}
      type={column.type === "number" ? "number" : "text"}
      value={value ?? ""}
      onChange={(e) => onChange(column.type === "number" ? Number(e.target.value) : e.target.value)}
    />
  );
}

function CrudTable({ tab }: { tab: Tab }) {
  const client = useQueryClient();
  const { data: rows } = useQuery({ queryKey: ["admin", tab.resource], queryFn: () => api<Row[]>(`/admin/${tab.resource}`) });
  const [editingId, setEditingId] = useState<string | number | null>(null);
  const [draft, setDraft] = useState<Row>({});
  const [error, setError] = useState("");

  const start = (row: Row | null) => {
    setError("");
    setEditingId(row ? row.id : "new");
    setDraft(row ? { ...row } : { ...tab.blank });
  };
  const save = async () => {
    try {
      const body: Row = { ...draft };
      for (const column of tab.columns) {
        if (column.type === "json" && typeof body[column.key] === "string") body[column.key] = JSON.parse(body[column.key]);
        if (column.type === "text" && body[column.key] === "") body[column.key] = column.key === "controller_type" ? null : "";
      }
      if (editingId === "new") await api(`/admin/${tab.resource}`, { method: "POST", body });
      else await api(`/admin/${tab.resource}/${editingId}`, { method: "PUT", body });
      setEditingId(null);
      await client.invalidateQueries();
    } catch (e) {
      setError(e instanceof ApiError || e instanceof Error ? e.message : t.common.error);
    }
  };
  const remove = async (row: Row) => {
    if (!window.confirm(t.admin.confirmDelete)) return;
    try {
      await api(`/admin/${tab.resource}/${row.id}`, { method: "DELETE" });
      await client.invalidateQueries();
    } catch (e) {
      setError(e instanceof Error ? e.message : t.common.error);
    }
  };

  const button = "rounded bg-slate-700 px-3 py-1 text-sm hover:bg-slate-600";
  const renderRow = (row: Row, isNew = false) => {
    const editing = isNew || editingId === row.id;
    const source = editing ? draft : row;
    return (
      <tr key={isNew ? "new" : row.id} className="border-t border-slate-800 align-middle">
        {tab.columns.map((column) => (
          <td key={column.key} className="px-2 py-1">
            <Cell column={column} value={source[column.key]} editing={editing} onChange={(v) => setDraft((d) => ({ ...d, [column.key]: v }))} />
          </td>
        ))}
        <td className="whitespace-nowrap px-2 py-1 text-right">
          {editing ? (
            <span className="space-x-2">
              <button className="rounded bg-sky-600 px-3 py-1 text-sm hover:bg-sky-500" onClick={save}>
                {t.common.save}
              </button>
              <button className={button} onClick={() => setEditingId(null)}>
                {t.common.cancel}
              </button>
            </span>
          ) : (
            <span className="space-x-2">
              <button className={button} onClick={() => start(row)}>
                {t.common.edit}
              </button>
              {tab.canDelete !== false && (
                <button className={button} onClick={() => remove(row)}>
                  {t.common.delete}
                </button>
              )}
            </span>
          )}
        </td>
      </tr>
    );
  };

  return (
    <div className="rounded-xl bg-slate-900 p-4">
      {error && <p className="mb-2 rounded bg-red-900/60 px-3 py-2 text-sm text-red-200">{error}</p>}
      <table className="w-full text-sm">
        <thead className="text-left text-slate-400">
          <tr>
            {tab.columns.map((column) => (
              <th key={column.key} className="px-2 py-1 font-medium">
                {column.label}
              </th>
            ))}
            <th />
          </tr>
        </thead>
        <tbody>
          {(rows ?? []).map((row) => renderRow(row))}
          {editingId === "new" && renderRow({}, true)}
        </tbody>
      </table>
      {tab.canCreate !== false && editingId === null && (
        <button className="mt-3 rounded bg-sky-600 px-3 py-2 text-sm font-medium hover:bg-sky-500" onClick={() => start(null)}>
          {t.common.add}
        </button>
      )}
    </div>
  );
}

export function AdminPage() {
  const { data: areas } = useQuery({ queryKey: ["admin", "areas"], queryFn: () => api<Row[]>("/admin/areas") });
  const { data: reasons } = useQuery({ queryKey: ["admin", "downtime-reasons"], queryFn: () => api<Row[]>("/admin/downtime-reasons") });
  const { data: machines } = useQuery({ queryKey: ["admin", "machines"], queryFn: () => api<Row[]>("/admin/machines") });
  const tabs = useTabs(areas ?? [], reasons ?? [], machines ?? []);
  const [active, setActive] = useState("machines");
  const [notice, setNotice] = useState("");
  const tab = tabs.find((x) => x.key === active) ?? tabs[0];

  const recalc = async () => {
    await api("/recalc?hours=24", { method: "POST" });
    setNotice(t.admin.recalcDone);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {tabs.map((x) => (
          <button
            key={x.key}
            onClick={() => setActive(x.key)}
            className={`rounded px-3 py-2 text-sm font-medium ${x.key === active ? "bg-sky-600" : "bg-slate-800 hover:bg-slate-700"}`}
          >
            {x.label}
          </button>
        ))}
        <button className="ml-auto rounded bg-slate-700 px-3 py-2 text-sm hover:bg-slate-600" onClick={recalc}>
          {t.admin.recalc}
        </button>
      </div>
      <p className="text-sm text-slate-400">{notice || t.admin.recalcHint}</p>
      <CrudTable key={tab.key} tab={tab} />
    </div>
  );
}
