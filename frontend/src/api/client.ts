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

export const query = (params: Record<string, string | number | string[] | undefined | null>) => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) value.forEach((v) => search.append(key, v));
    else search.append(key, String(value));
  }
  return `?${search.toString()}`;
};

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
  machine_id: string;
  name: string;
  site_id: string;
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
  id: string;
  name: string;
  site_id: string;
  area_id: number | null;
  area_name: string | null;
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
  machine_id: string | null;
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
  machine_id: string;
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
