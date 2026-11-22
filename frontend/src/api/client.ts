// Thin typed wrapper over the REST API.

const TOKEN_KEY = "mm_token";

// Kiosks and TV dashboards are opened as <page>?token=<device token>.
const urlToken = new URLSearchParams(window.location.search).get("token");
if (urlToken) localStorage.setItem(TOKEN_KEY, urlToken);

export const getToken = () => localStorage.getItem(TOKEN_KEY);
export const setToken = (token: string | null) =>
  token ? localStorage.setItem(TOKEN_KEY, token) : localStorage.removeItem(TOKEN_KEY);

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, options: { method?: string; body?: unknown } = {}): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    method: options.method ?? "GET",
    headers: {
      ...(options.body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
    },
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });
  if (response.status === 401 && !path.startsWith("/auth/login")) {
    setToken(null);
    const next = encodeURIComponent(window.location.pathname);
    window.location.assign(`/login?next=${next}`);
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    const message = typeof detail?.detail === "string" ? detail.detail : response.statusText;
    throw new ApiError(response.status, message);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

export const query = (params: Record<string, string | number | (string | number)[] | undefined | null>) => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) value.forEach((v) => search.append(key, String(v)));
    else search.append(key, String(value));
  }
  return `?${search.toString()}`;
};

export interface AreaNode {
  id: number;
  site_id: number;
  name: string;
}

export interface SiteNode {
  id: number;
  org_id: number;
  code: string;
  name: string;
  timezone: string;
  areas: AreaNode[];
}

export interface Me {
  name: string;
  kind: "user" | "device";
  superadmin: boolean;
  roles: string[];
  menu: {
    dashboard: boolean;
    analytics: boolean;
    references: boolean;
    structure: boolean;
    users: boolean;
    service: boolean;
  };
  kiosk: { machine_id: number | null; area_id: number | null } | null;
  organizations: { id: number; code: string; name: string }[];
  sites: SiteNode[];
}

export interface ShiftLive {
  name: string;
  start: string;
  end: string;
  total_parts: number;
  good_parts: number;
  scrap_parts: number;
  plan_now: number | null;
  plan_shift: number | null;
  availability: number | null;
  performance: number | null;
  quality: number | null;
  oee: number | null;
}

export interface Live {
  machine_id: number;
  code: string;
  name: string;
  org_id: number;
  site_id: number;
  site_name: string;
  area_id: number | null;
  area_name: string | null;
  state: string;
  since: string | null;
  downtime_since: string | null;
  reason: { id: number; name: string; color: string; category: string } | null;
  setup_active: boolean;
  program: string | null;
  mode: string | null;
  spindle_load: number | null;
  part_count: number | null;
  last_ts: string | null;
  shift: ShiftLive | null;
}

export interface Machine {
  id: number;
  code: string;
  name: string;
  org_id: number;
  org_name: string;
  site_id: number;
  site_name: string;
  timezone: string;
  area_id: number | null;
  area_name: string | null;
  controller_type: string | null;
  live: Live | null;
}

export interface Reason {
  id: number;
  parent_id: number | null;
  code: string;
  name: string;
  category: string;
  color: string;
  children?: Reason[];
}

export interface OeeRow {
  machine_id: number | null;
  period_start: string;
  period_end: string;
  label: string;
  planned_time_s: number;
  run_time_s: number;
  total_parts: number;
  good_parts: number;
  scrap_parts: number;
  no_standard: boolean;
  availability: number | null;
  performance: number | null;
  quality: number | null;
  oee: number | null;
}

export interface Interval {
  machine_id: number;
  state: string;
  start_ts: string;
  end_ts: string;
  program: string | null;
}

export interface ParetoRow {
  reason_id: number | null;
  state: string | null;
  name: string | null;
  group_name: string | null;
  category: string | null;
  color: string | null;
  duration_s: number;
  count: number;
}

export interface Shift {
  name: string;
  start: string;
  end: string;
}

export interface Area {
  id: number;
  name: string;
}

/** Upload a file (multipart). */
export async function upload<T>(path: string, file: File): Promise<T> {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(`/api/v1${path}`, {
    method: "POST",
    headers: getToken() ? { Authorization: `Bearer ${getToken()}` } : {},
    body: form,
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => null);
    throw new ApiError(response.status, typeof detail?.detail === "string" ? detail.detail : response.statusText);
  }
  return (await response.json()) as T;
}

/** Download a protected file: the browser cannot send the token with a plain link. */
export async function download(path: string, filename: string): Promise<void> {
  const response = await fetch(`/api/v1${path}`, { headers: getToken() ? { Authorization: `Bearer ${getToken()}` } : {} });
  if (!response.ok) throw new ApiError(response.status, response.statusText);
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

export interface CatalogParameter {
  code: string;
  unit: string;
  group: "iso230_4" | "diagnostics";
  per_axis: boolean;
  directions: (string | null)[];
}

export interface Catalog {
  planes: string[];
  plane_axes: Record<string, string[]>;
  arcs: number[];
  parameters: CatalogParameter[];
}

export interface BallbarResult {
  parameter: string;
  axis: string | null;
  direction: string | null;
  value: number;
  unit: string;
}

export interface BallbarTest {
  id: number;
  machine_id: number;
  performed_at: string;
  engineer: string;
  plane: string;
  radius_mm: number;
  feed_mm_min: number;
  arc_deg: number;
  air_temp_c: number | null;
  machine_temp_c: number | null;
  template: string | null;
  notes: string | null;
  voided_at: string | null;
  void_reason: string | null;
  results_count?: number;
  files?: number | BallbarFile[];
  has_traces?: boolean;
  results?: BallbarResult[];
  traces?: { direction: string; points: number; source: string }[];
}

export interface BallbarFile {
  id: number;
  kind: string;
  filename: string;
  size_bytes: number;
  sha256: string;
  uploaded_by_name: string;
  uploaded_at: string;
}

export interface TrendGroup {
  key: string;
  template: string | null;
  plane: string;
  radius_mm: number;
  feed_mm_min: number;
  arc_deg: number;
  tests: number;
  last: string;
}

export interface TrendEvent {
  kind: "collision" | "repair";
  ts: string;
  text: string | null;
}

export interface Trend {
  unit: string;
  points: { test_id: number; performed_at: string; value: number }[];
  events: TrendEvent[];
}

export interface ArchiveRow {
  machine_id: number;
  machine_name: string;
  org_name: string;
  site_name: string;
  area_name: string | null;
  last_test_at: string | null;
  days_since: number | null;
  tests: number;
}

export interface Repair {
  id: number;
  completed_at: string;
  description: string;
  engineer: string;
  voided_at: string | null;
  void_reason: string | null;
}

export interface CollisionMark {
  id: number;
  ts: string;
  comment: string | null;
  operator: string | null;
  voided_at: string | null;
  void_reason: string | null;
}
