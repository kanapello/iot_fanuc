import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type Me, type SiteNode } from "../api/client";
import { t } from "./i18n";

/** Who is signed in and which part of the hierarchy they see (GET /auth/me). */
export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => api<Me>("/auth/me"), staleTime: 60_000 });
}

export const roleLabel = (role: string) => t.roles[role] ?? role;

/** Human name of a hierarchy node. */
export function nodeLabel(me: Me | undefined, orgId: number | null, siteId?: number | null, areaId?: number | null): string {
  if (!me) return "";
  const org = me.organizations.find((o) => o.id === orgId)?.name ?? (orgId === null ? t.users.allOrganizations : `#${orgId}`);
  const site = siteId ? me.sites.find((s) => s.id === siteId) : undefined;
  const area = areaId ? site?.areas.find((a) => a.id === areaId) : undefined;
  return [org, site?.name, area?.name].filter(Boolean).join(" → ");
}

const STORAGE_KEY = "mm_node";

function load(): { siteId: number | null; areaId: number | null } {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}");
    return { siteId: parsed.siteId ?? null, areaId: parsed.areaId ?? null };
  } catch {
    return { siteId: null, areaId: null };
  }
}

/** Selected site and area; remembered per browser, always kept inside what the user may see. */
export function useNodeSelection(sites: SiteNode[] | undefined, options: { requireSite?: boolean } = {}) {
  const [selection, setSelection] = useState(load);
  const site = sites?.find((s) => s.id === selection.siteId) ?? (options.requireSite ? sites?.[0] : undefined);
  const area = site?.areas.find((a) => a.id === selection.areaId);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(selection));
    } catch {
      /* private mode: selection is just not remembered */
    }
  }, [selection]);

  return {
    siteId: site?.id ?? null,
    areaId: area?.id ?? null,
    site,
    setSite: (siteId: number | null) => setSelection({ siteId, areaId: null }),
    setArea: (areaId: number | null) => setSelection((current) => ({ siteId: site?.id ?? current.siteId, areaId })),
  };
}

/** Site and area pickers; shown only when the user has more than one choice. */
export function NodePicker({
  sites,
  selection,
  allowAllSites = true,
  large = false,
}: {
  sites: SiteNode[];
  selection: ReturnType<typeof useNodeSelection>;
  allowAllSites?: boolean;
  large?: boolean;
}) {
  const select = `rounded border border-slate-700 bg-slate-800 px-3 ${large ? "py-3 text-xl" : "py-2 text-sm"}`;
  const areas = selection.site?.areas ?? [];
  const showSites = sites.length > 1;
  const showAreas = areas.length > 1;
  if (!showSites && !showAreas) return null;
  return (
    <div className="flex flex-wrap items-center gap-2">
      {showSites && (
        <select
          className={select}
          value={selection.siteId ?? ""}
          onChange={(e) => selection.setSite(e.target.value ? Number(e.target.value) : null)}
        >
          {allowAllSites && <option value="">{t.common.allSites}</option>}
          {sites.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
      )}
      {showAreas && (
        <select
          className={select}
          value={selection.areaId ?? ""}
          onChange={(e) => selection.setArea(e.target.value ? Number(e.target.value) : null)}
        >
          <option value="">{t.common.allAreas}</option>
          {areas.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}
