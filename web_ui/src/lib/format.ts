// Formatting helpers shared across pages — durations, timestamps, statuses.

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}с`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}м ${Math.round(seconds % 60)}с`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}ч ${m % 60}м`;
  const d = Math.floor(h / 24);
  return `${d}д ${h % 24}ч`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(d);
}

export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "только что";
  if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} ч назад`;
  return `${Math.floor(diff / 86400)} дн назад`;
}

export function shortId(id: string | null | undefined, len = 8): string {
  if (!id) return "—";
  return id.length > len ? `${id.slice(0, len)}…` : id;
}

export type Tone = "valid" | "falsealarm" | "expected" | "danger" | "live" | "muted";

export interface StatusDescriptor {
  label: string;
  tone: Tone;
}

/** Map a ticket's raw fields to a single human lifecycle status + a tone. */
export function lifecycleStatus(t: {
  creation_status: string;
  confirmation_status: string;
  valid_incident: boolean;
  resolved_at: string | null;
  jira_issue_key: string | null;
}): StatusDescriptor {
  if (t.creation_status === "failed_jira" || t.confirmation_status === "failed_confirmation")
    return { label: "Ошибка", tone: "danger" };
  if (t.resolved_at) return { label: "Закрыт", tone: "muted" };
  if (t.valid_incident) return { label: "Открыт", tone: "live" };
  if (t.confirmation_status === "pending_confirmation")
    return { label: "Подтверждается", tone: "expected" };
  if (!t.jira_issue_key) return { label: "Без Jira", tone: "falsealarm" };
  return { label: "Новый", tone: "muted" };
}

const VALIDITY_TONES: Record<string, Tone> = {
  Валидный: "valid",
  Ложный: "falsealarm",
  Ожидаемый: "expected",
};

export function validityTone(label: string | null | undefined): Tone {
  if (!label) return "muted";
  return VALIDITY_TONES[label] ?? "muted";
}

export const VALIDITY_OPTIONS = ["Валидный", "Ложный", "Ожидаемый"];
