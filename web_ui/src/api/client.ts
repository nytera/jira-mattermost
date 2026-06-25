import type {
  AdminStats,
  AlertsResponse,
  AlertTicket,
  ConfirmationResult,
  CreateFromLinkResult,
  FeedbackItem,
  LogsResponse,
  RecreateResult,
  SettingsResponse,
} from "./types";

const TOKEN_KEY = "mmjira.admin.token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

/** Thrown for non-2xx responses so callers can branch on the HTTP status. */
export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

/** A 401 means the stored token is wrong/expired — the auth layer listens for
 * this to bounce back to the login screen. */
type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler | null = null;
export function setUnauthorizedHandler(handler: UnauthorizedHandler): void {
  onUnauthorized = handler;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(`/admin/api${path}`, { ...init, headers });

  if (response.status === 401) {
    onUnauthorized?.();
    throw new ApiError(401, "Неверный или просроченный токен.");
  }

  let payload: unknown = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }

  if (!response.ok) {
    const detail =
      (payload && typeof payload === "object" && "detail" in payload
        ? String((payload as { detail: unknown }).detail)
        : null) ?? `Ошибка ${response.status}`;
    throw new ApiError(response.status, detail, payload);
  }

  return payload as T;
}

const get = <T>(path: string) => request<T>(path);
const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });

export const api = {
  // Probe used by the login form to validate a token.
  verifyToken: () => get<{ total: number }>("/summary"),

  stats: () => get<AdminStats>("/stats"),
  alerts: (params: { limit?: number; status?: string; validity?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.limit) q.set("limit", String(params.limit));
    if (params.status) q.set("status", params.status);
    if (params.validity) q.set("validity", params.validity);
    const qs = q.toString();
    return get<AlertsResponse>(`/alerts${qs ? `?${qs}` : ""}`);
  },
  alert: (postId: string) => get<AlertTicket>(`/alerts/${postId}`),
  feedback: (postId: string) => get<{ feedback: FeedbackItem[] }>(`/alerts/${postId}/feedback`),
  logs: (params: { limit?: number; level?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.limit) q.set("limit", String(params.limit));
    if (params.level) q.set("level", params.level);
    const qs = q.toString();
    return get<LogsResponse>(`/logs${qs ? `?${qs}` : ""}`);
  },
  settings: () => get<SettingsResponse>("/settings"),
  saveSetting: (key: string, value: string) =>
    post<SettingsResponse>(`/settings/${key}`, { value }),
  resetSetting: (key: string) => post<SettingsResponse>(`/settings/${key}/reset`),

  createFromLink: (link: string) =>
    post<CreateFromLinkResult>("/alerts/create-from-link", { link }),
  recreate: (postId: string, force = false) =>
    post<RecreateResult>(`/alerts/${postId}/jira/recreate${force ? "?force=true" : ""}`),
  confirm: (postId: string) => post<ConfirmationResult>(`/alerts/${postId}/confirm`),
  end: (postId: string, endedAt?: string) =>
    post<ConfirmationResult>(`/alerts/${postId}/end`, endedAt ? { ended_at: endedAt } : {}),
  setValidity: (postId: string, validityLabel: string) =>
    post<ConfirmationResult>(`/alerts/${postId}/validity`, { validity_label: validityLabel }),
  postmortem: (postId: string) => post<ConfirmationResult>(`/alerts/${postId}/postmortem`),
  summary: (postId: string) => post<{ message: string }>(`/alerts/${postId}/summary`),
};
