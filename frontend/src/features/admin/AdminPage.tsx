import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, query, type Machine, type Me } from "../../api/client";
import { t } from "../../shared/i18n";
import { useMe } from "../../shared/session";
import { firstNode, inputClass, NodeForm, type NodeValue } from "./NodeForm";
import { TokensPanel } from "./TokensPanel";
import { UsersPanel } from "./UsersPanel";

type Row = Record<string, any>;
type ColumnType = "text" | "number" | "bool" | "select" | "json" | "readonly";
interface Column {
  key: string;
  label: string;
  type: ColumnType;
  options?: { value: string | number | null; label: string }[];
}
/** What node a table belongs to: a site, an organization (optionally narrowed to a site) or the whole installation. */
type NodeKind = "site" | "orgOrSite" | "org" | "none";
interface Tab {
  key: string;
  label: string;
  resource?: string;
  node: NodeKind;
  columns?: Column[];
  blank?: Row;
  canCreate?: boolean;
}

const c = t.admin.col;

function buildTabs(me: Me, node: NodeValue, rows: { reasons: Row[]; machines: Machine[] }): Tab[] {
  const site = me.sites.find((s) => s.id === node.site_id);
  const areaOptions = [{ value: null, label: "—" }, ...(site?.areas ?? []).map((a) => ({ value: a.id, label: a.name }))];
  const groupOptions = [
    { value: null, label: "— (группа)" },
    ...rows.reasons.filter((r) => r.parent_id === null).map((r) => ({ value: r.id, label: r.name })),
  ];
  const machineOptions = [
    { value: null, label: "—" },
    ...rows.machines
      .filter((m) => m.org_id === node.org_id && (node.site_id === null || m.site_id === node.site_id))
      .map((m) => ({ value: m.id, label: m.name })),
  ];
  const categories = Object.entries(t.admin.category).map(([value, label]) => ({ value, label }));
  const isAdmin = me.superadmin || me.roles.includes("admin");
  const tabs: Tab[] = [];
  if (me.menu.structure) {
    tabs.push(
      {
        key: "machines",
        label: t.admin.machines,
        resource: "machines",
        node: "site",
        canCreate: false,
        columns: [
          { key: "code", label: c.id, type: "readonly" },
          { key: "name", label: c.name, type: "text" },
          { key: "area_id", label: c.area, type: "select", options: areaOptions },
          { key: "model", label: c.model, type: "text" },
          { key: "controller_type", label: c.controller, type: "text" },
          { key: "sort_order", label: c.order, type: "number" },
          { key: "is_active", label: c.active, type: "bool" },
        ],
      },
      {
        key: "areas",
        label: t.admin.areas,
        resource: "areas",
        node: "site",
        blank: { name: "" },
        columns: [{ key: "name", label: c.name, type: "text" }],
      },
      {
        key: "collectors",
        label: t.admin.collectors,
        resource: "collectors",
        node: "site",
        blank: { id: "", name: "" },
        columns: [
          { key: "id", label: c.collectorId, type: "text" },
          { key: "name", label: c.name, type: "text" },
          { key: "version", label: "Версия", type: "readonly" },
        ],
      },
      { key: "tokens", label: t.tokens.title, node: "none" },
      {
        key: "sites",
        label: t.admin.sites,
        resource: "sites",
        node: "org",
        blank: { code: "", name: "", timezone: "Europe/Moscow" },
        columns: [
          { key: "code", label: c.code, type: "text" },
          { key: "name", label: c.name, type: "text" },
          { key: "timezone", label: c.timezone, type: "text" },
        ],
      },
    );
    if (me.superadmin) {
      tabs.push({
        key: "organizations",
        label: t.admin.organizations,
        resource: "organizations",
        node: "none",
        blank: { code: "", name: "" },
        columns: [
          { key: "code", label: c.code, type: "text" },
          { key: "name", label: c.name, type: "text" },
        ],
      });
    }
  }
  if (me.menu.references) {
    tabs.push(
      {
        key: "shifts",
        label: t.admin.shifts,
        resource: "shift-templates",
        node: "orgOrSite",
        canCreate: isAdmin,
        blank: { name: "", start_time: "08:00:00", end_time: "20:00:00", days_of_week: [1, 2, 3, 4, 5, 6, 7], planned_breaks: [], is_active: true },
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
        node: "orgOrSite",
        canCreate: isAdmin,
        blank: { parent_id: null, code: "", name: "", category: "unplanned", color: "#64748b", sort_order: 0, is_active: true },
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
        node: "orgOrSite",
        canCreate: isAdmin,
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
        node: "orgOrSite",
        canCreate: isAdmin || me.roles.includes("engineer"),
        blank: { machine_id: null, program: "", ideal_cycle_time_s: 60, parts_per_cycle: 1 },
        columns: [
          { key: "program", label: c.program, type: "text" },
          { key: "machine_id", label: c.machine, type: "select", options: machineOptions },
          { key: "ideal_cycle_time_s", label: c.cycle, type: "number" },
          { key: "parts_per_cycle", label: c.partsPerCycle, type: "number" },
          { key: "valid_from", label: c.validFrom, type: "readonly" },
        ],
      },
    );
  }
  if (me.menu.users) tabs.push({ key: "users", label: t.users.title, node: "none" });
  return tabs;
}

function Cell({ column, value, editing, onChange }: { column: Column; value: any; editing: boolean; onChange: (v: any) => void }) {
  if (!editing || column.type === "readonly") {
    if (column.type === "bool") return <>{value ? t.common.yes : t.common.no}</>;
    if (column.type === "select") return <>{column.options?.find((o) => o.value === value)?.label ?? value ?? "—"}</>;
    if (column.type === "json") return <code className="text-xs">{JSON.stringify(value)}</code>;
    if (column.key === "valid_from" && value) return <>{new Date(value).toLocaleString("ru-RU")}</>;
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
        className={`${inputClass} w-full`}
        value={value === null || value === undefined ? "" : String(value)}
        onChange={(e) => onChange(column.options?.find((o) => String(o.value ?? "") === e.target.value)?.value ?? null)}
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
    return (
      <input
        className={`${inputClass} w-full font-mono text-xs`}
        value={typeof value === "string" ? value : JSON.stringify(value)}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  return (
    <input
      className={`${inputClass} w-full`}
      type={column.type === "number" ? "number" : "text"}
      value={value ?? ""}
      onChange={(e) => onChange(column.type === "number" ? Number(e.target.value) : e.target.value)}
    />
  );
}

function CrudTable({ tab, node }: { tab: Tab; node: NodeValue }) {
  const client = useQueryClient();
  const resource = tab.resource!;
  const columns = tab.columns ?? [];
  // rows of exactly the selected node: a site, or the organization defaults
  const params =
    tab.node === "site" ? { site_id: node.site_id } : tab.node === "none" ? {} : { org_id: node.org_id, site_id: node.site_id };
  const path = `/admin/${resource}${query(params)}`;
  const { data: rows } = useQuery({ queryKey: ["admin", path], queryFn: () => api<Row[]>(path) });
  const [editingId, setEditingId] = useState<string | number | null>(null);
  const [draft, setDraft] = useState<Row>({});
  const [error, setError] = useState("");

  const nodeFields: Row =
    tab.node === "site" ? { site_id: node.site_id } : tab.node === "none" ? {} : { org_id: node.org_id, site_id: tab.node === "org" ? undefined : node.site_id };

  const start = (row: Row | null) => {
    setError("");
    setEditingId(row ? row.id : "new");
    setDraft(row ? { ...row } : { ...tab.blank });
  };
  const save = async () => {
    try {
      const body: Row = {};
      for (const [key, value] of Object.entries(draft)) if (!key.startsWith("_")) body[key] = value;
      for (const column of columns) {
        if (column.type === "json" && typeof body[column.key] === "string") body[column.key] = JSON.parse(body[column.key]);
      }
      if (editingId === "new") await api(`/admin/${resource}`, { method: "POST", body: { ...body, ...nodeFields } });
      else await api(`/admin/${resource}/${editingId}`, { method: "PUT", body });
      setEditingId(null);
      await client.invalidateQueries();
    } catch (e) {
      setError(e instanceof Error ? e.message : t.common.error);
    }
  };
  const remove = async (row: Row) => {
    if (!window.confirm(t.admin.confirmDelete)) return;
    try {
      await api(`/admin/${resource}/${row.id}`, { method: "DELETE" });
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
        {columns.map((column) => (
          <td key={column.key} className="px-2 py-1">
            <Cell
              column={column.key === "id" && !isNew ? { ...column, type: "readonly" } : column}
              value={source[column.key]}
              editing={editing}
              onChange={(v) => setDraft((d) => ({ ...d, [column.key]: v }))}
            />
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
          ) : row._editable ? (
            <span className="space-x-2">
              <button className={button} onClick={() => start(row)}>
                {t.common.edit}
              </button>
              {tab.canCreate !== false && (
                <button className={button} onClick={() => remove(row)}>
                  {t.common.delete}
                </button>
              )}
            </span>
          ) : (
            <span className="text-xs text-slate-500">{t.admin.readOnly}</span>
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
            {columns.map((column) => (
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
  const { data: me } = useMe();
  const [active, setActive] = useState<string | null>(null);
  const [node, setNode] = useState<NodeValue | null>(null);
  const [notice, setNotice] = useState("");
  const current = node ?? (me ? firstNode(me, true) : { org_id: null, site_id: null, area_id: null });
  const { data: machines } = useQuery({ queryKey: ["machines"], queryFn: () => api<Machine[]>("/machines"), enabled: Boolean(me) });
  const reasonsPath = `/admin/downtime-reasons${query({ org_id: current.org_id, site_id: current.site_id })}`;
  const { data: reasons } = useQuery({
    queryKey: ["admin", reasonsPath],
    queryFn: () => api<Row[]>(reasonsPath),
    enabled: Boolean(me?.menu.references),
  });
  if (!me) return <p className="text-slate-400">{t.common.loading}</p>;
  const tabs = buildTabs(me, current, { reasons: reasons ?? [], machines: machines ?? [] });
  if (!tabs.length) return <p className="text-slate-400">{t.common.noAccess}</p>;
  const tab = tabs.find((x) => x.key === active) ?? tabs[0];

  // a site table needs a site; an organization-level reference may also be narrowed to a site
  const needSite = tab.node === "site";
  const nodeValue = needSite && current.site_id === null ? firstNode(me, true) : current;

  const recalc = async () => {
    await api(`/recalc${query({ hours: 24, site_id: nodeValue.site_id })}`, { method: "POST" });
    setNotice(t.admin.recalcDone);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {tabs.map((x) => (
          <button
            key={x.key}
            onClick={() => setActive(x.key)}
            className={`rounded px-3 py-2 text-sm font-medium ${x.key === tab.key ? "bg-sky-600" : "bg-slate-800 hover:bg-slate-700"}`}
          >
            {x.label}
          </button>
        ))}
        {me.menu.structure && (
          <button className="ml-auto rounded bg-slate-700 px-3 py-2 text-sm hover:bg-slate-600" onClick={recalc}>
            {t.admin.recalc}
          </button>
        )}
      </div>
      {tab.node !== "none" && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-slate-400">{t.admin.node}:</span>
          <NodeForm
            me={me}
            value={nodeValue}
            onChange={setNode}
            depth={tab.node === "org" ? "org" : "site"}
            allowWholeOrg={!needSite}
          />
          {tab.node === "orgOrSite" && current.site_id === null && <span className="text-slate-400">({t.admin.orgDefaults})</span>}
        </div>
      )}
      <p className="text-sm text-slate-400">{notice || t.admin.recalcHint}</p>
      {tab.key === "users" ? (
        <UsersPanel me={me} />
      ) : tab.key === "tokens" ? (
        <TokensPanel me={me} />
      ) : (
        <CrudTable key={`${tab.key}:${nodeValue.org_id}:${nodeValue.site_id}`} tab={tab} node={nodeValue} />
      )}
    </div>
  );
}
