import type { Me } from "../../api/client";
import { t } from "../../shared/i18n";

export interface NodeValue {
  org_id: number | null;
  site_id: number | null;
  area_id: number | null;
}

export const inputClass = "rounded border border-slate-600 bg-slate-800 px-2 py-1";

/** Organization → site → area selectors limited to what the user sees. */
export function NodeForm({
  me,
  value,
  onChange,
  depth = "area",
  allowWholeOrg = true,
}: {
  me: Me;
  value: NodeValue;
  onChange: (value: NodeValue) => void;
  depth?: "org" | "site" | "area";
  allowWholeOrg?: boolean;
}) {
  const sites = me.sites.filter((s) => s.org_id === value.org_id);
  const areas = sites.find((s) => s.id === value.site_id)?.areas ?? [];
  const number = (v: string) => (v ? Number(v) : null);
  return (
    <span className="inline-flex flex-wrap gap-2">
      <select
        className={inputClass}
        value={value.org_id ?? ""}
        onChange={(e) => onChange({ org_id: number(e.target.value), site_id: null, area_id: null })}
      >
        {me.organizations.map((o) => (
          <option key={o.id} value={o.id}>
            {o.name}
          </option>
        ))}
      </select>
      {depth !== "org" && (
        <select
          className={inputClass}
          value={value.site_id ?? ""}
          onChange={(e) => onChange({ ...value, site_id: number(e.target.value), area_id: null })}
        >
          {allowWholeOrg && <option value="">{t.users.wholeOrganization}</option>}
          {sites.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
      )}
      {depth === "area" && value.site_id !== null && (
        <select
          className={inputClass}
          value={value.area_id ?? ""}
          onChange={(e) => onChange({ ...value, area_id: number(e.target.value) })}
        >
          <option value="">{t.users.wholeSite}</option>
          {areas.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
      )}
    </span>
  );
}

export function firstNode(me: Me, needSite: boolean): NodeValue {
  const org = me.organizations[0]?.id ?? null;
  const site = needSite ? (me.sites.find((s) => s.org_id === org)?.id ?? null) : null;
  return { org_id: org, site_id: site, area_id: null };
}
