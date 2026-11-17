import { useQuery } from "@tanstack/react-query";
import { api, type BallbarResult, type Catalog, type CatalogParameter, type Me } from "../../api/client";
import { t } from "../../shared/i18n";

export function useCatalog() {
  return useQuery({ queryKey: ["geometry-catalog"], queryFn: () => api<Catalog>("/geometry/catalog"), staleTime: Infinity });
}

/** Whether the user enters ballbar tests (the server checks it per machine). */
export const canEditGeometry = (me: Me | undefined) =>
  Boolean(me && (me.superadmin || me.roles.some((r) => ["engineer", "admin", "service"].includes(r))));

export const directionLabel = (direction: string | null) => t.geometry.directions[direction ?? ""] ?? direction ?? "";

/** "Люфт X", "Круглость (по ч. с.)", "Круглость (общая)". */
export function resultLabel(parameter: string, axis: string | null, direction: string | null, catalog?: Catalog): string {
  const name = t.geometry.params[parameter] ?? parameter;
  const entry = catalog?.parameters.find((p) => p.code === parameter);
  const withDirections = entry ? entry.directions.length > 1 : direction !== null;
  return `${name}${axis ? ` ${axis}` : ""}${withDirections ? ` (${directionLabel(direction)})` : ""}`;
}

/** Every (axis, direction) a parameter has in a plane. */
export function keysOf(parameter: CatalogParameter, plane: string, catalog: Catalog): [string | null, string | null][] {
  const axes: (string | null)[] = parameter.per_axis ? catalog.plane_axes[plane] ?? [] : [null];
  return axes.flatMap((axis) => parameter.directions.map((direction) => [axis, direction] as [string | null, string | null]));
}

export const resultKey = (parameter: string, axis: string | null, direction: string | null) =>
  `${parameter}|${axis ?? ""}|${direction ?? ""}`;

export const formatNumber = (value: number | null | undefined, digits = 1) =>
  value === null || value === undefined ? "—" : value.toLocaleString("ru-RU", { maximumFractionDigits: digits });

export const formatDate = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleDateString("ru-RU") : "—");

export const exceededText = (r: BallbarResult) =>
  `${resultLabel(r.parameter, r.axis, r.direction)}: ${formatNumber(r.value, 2)} ${r.unit} > ${formatNumber(r.limit, 2)}`;

/** At most ``limit`` triggers, the rest as a count: a recurring alarm must not flood the screen. */
export function shortTriggers<T>(triggers: T[], limit = 3): { shown: T[]; more: number } {
  return { shown: triggers.slice(0, limit), more: Math.max(0, triggers.length - limit) };
}

export const STATUS_COLORS: Record<string, string> = {
  never: "#64748b",
  ok: "#22c55e",
  soon: "#eab308",
  overdue: "#ef4444",
  event: "#a855f7",
};
