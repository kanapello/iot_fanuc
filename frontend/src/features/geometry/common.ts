import { useQuery } from "@tanstack/react-query";
import { api, type Catalog, type CatalogParameter, type Me } from "../../api/client";
import { t } from "../../shared/i18n";

/** Catalog of ballbar parameters; it exists only for the service, so it is requested only for them. */
export function useCatalog(enabled = true) {
  return useQuery({
    queryKey: ["geometry-catalog"],
    queryFn: () => api<Catalog>("/geometry/catalog"),
    staleTime: Infinity,
    enabled,
  });
}

/** The geometry archive exists only for the service (and the superadmin). */
export const canUseArchive = (me: Me | undefined) => Boolean(me?.menu.service);

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

/** Whole days since a moment; the same rule as the server's archive index. */
export const daysSince = (iso: string) => Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / 86_400_000));
