import { useState } from "react";
import {
  CheckCircle2,
  Siren,
  FlagTriangleRight,
  FileText,
  ListChecks,
  ExternalLink,
} from "lucide-react";
import { api, ApiError } from "@/api/client";
import { useApi } from "@/lib/useApi";
import { useToast } from "@/components/Toast";
import type { AlertTicket } from "@/api/types";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  SectionHeader,
  Spinner,
} from "@/components/ui";
import { SlideOver, Field } from "@/components/SlideOver";
import {
  formatDateTime,
  formatRelative,
  lifecycleStatus,
  validityTone,
  VALIDITY_OPTIONS,
} from "@/lib/format";

const FILTERS = [
  { key: "all", label: "Все" },
  { key: "open", label: "Открытые" },
  { key: "empty", label: "Без валидности" },
];

export default function Incidents() {
  const toast = useToast();
  const [filter, setFilter] = useState("all");
  const [selected, setSelected] = useState<string | null>(null);
  const list = useApi(
    () => api.alerts({ limit: 200, validity: filter === "empty" ? "empty" : undefined }),
    [filter],
  );

  const rows = (list.data?.alerts ?? []).filter((t) =>
    filter === "open" ? t.valid_incident && !t.resolved_at : true,
  );

  return (
    <div className="flex flex-col gap-5">
      <SectionHeader
        title="Инциденты"
        caption="Жизненный цикл: подтверждение, завершение, валидность, постмортем."
      />

      <div className="flex flex-wrap gap-1.5">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors ${
              filter === f.key
                ? "border-live/40 bg-live/10 text-live"
                : "border-line2 text-muted hover:text-fg"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      <div className="panel overflow-hidden">
        {list.loading ? (
          <Spinner label="Загрузка инцидентов…" />
        ) : list.error ? (
          <ErrorState message={list.error} onRetry={list.reload} />
        ) : rows.length === 0 ? (
          <EmptyState icon={<Siren size={28} />} title="Нет инцидентов" hint="Под текущий фильтр ничего не найдено." />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left">
                <th className="px-4 py-2.5 eyebrow font-normal">Алерт</th>
                <th className="px-4 py-2.5 eyebrow font-normal">Статус</th>
                <th className="hidden px-4 py-2.5 eyebrow font-normal md:table-cell">Валидность</th>
                <th className="hidden px-4 py-2.5 eyebrow font-normal lg:table-cell">Jira</th>
                <th className="hidden px-4 py-2.5 eyebrow font-normal lg:table-cell">Создан</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => {
                const st = lifecycleStatus(t);
                return (
                  <tr
                    key={t.id}
                    onClick={() => setSelected(t.mattermost_post_id)}
                    className="cursor-pointer border-b border-line/60 transition-colors last:border-0 hover:bg-white/[0.03]"
                  >
                    <td className="max-w-0 px-4 py-3">
                      <div className="truncate font-medium text-fg">
                        {t.mattermost_alert_title || t.mattermost_message_preview || "—"}
                      </div>
                      <div className="truncate font-mono text-[11px] text-faint">
                        {t.mattermost_post_id}
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <Badge tone={st.tone} live={st.tone === "live"}>
                        {st.label}
                      </Badge>
                    </td>
                    <td className="hidden px-4 py-3 md:table-cell">
                      {t.validity_status ? (
                        <Badge tone={validityTone(t.validity_status)}>{t.validity_status}</Badge>
                      ) : (
                        <span className="text-faint">—</span>
                      )}
                    </td>
                    <td className="hidden px-4 py-3 font-mono text-xs lg:table-cell">
                      {t.jira_issue_key ? (
                        <span className="text-muted">{t.jira_issue_key}</span>
                      ) : (
                        <span className="text-faint">—</span>
                      )}
                    </td>
                    <td className="hidden px-4 py-3 text-xs text-muted lg:table-cell">
                      {formatRelative(t.mattermost_message_created_at)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <IncidentDrawer
        postId={selected}
        onClose={() => setSelected(null)}
        onChanged={() => list.reload()}
        notify={(m, ok) => toast.push(m, ok ? "ok" : "error")}
      />
    </div>
  );
}

function IncidentDrawer({
  postId,
  onClose,
  onChanged,
  notify,
}: {
  postId: string | null;
  onClose: () => void;
  onChanged: () => void;
  notify: (message: string, ok: boolean) => void;
}) {
  const detail = useApi(() => (postId ? api.alert(postId) : Promise.resolve(null)), [postId]);
  const [busy, setBusy] = useState<string | null>(null);

  async function run(action: string, fn: () => Promise<{ message: string; status?: string; ok?: boolean }>) {
    setBusy(action);
    try {
      const res = await fn();
      const ok = res.ok ?? !["error", "not_found"].includes(res.status ?? "");
      notify(res.message || "Готово", ok);
      detail.reload();
      onChanged();
    } catch (e) {
      notify(e instanceof ApiError ? e.message : "Ошибка действия", false);
    } finally {
      setBusy(null);
    }
  }

  const t = detail.data;
  return (
    <SlideOver
      open={Boolean(postId)}
      onClose={onClose}
      title={t?.mattermost_alert_title || t?.mattermost_post_id || "Инцидент"}
      subtitle={t ? <span className="font-mono text-xs">{t.mattermost_post_id}</span> : undefined}
      footer={
        t && (
          <LifecycleActions
            ticket={t}
            busy={busy}
            onConfirm={() => run("confirm", () => api.confirm(t.mattermost_post_id))}
            onEnd={() => run("end", () => api.end(t.mattermost_post_id))}
            onPostmortem={() => run("postmortem", () => api.postmortem(t.mattermost_post_id))}
            onSummary={() => run("summary", () => api.summary(t.mattermost_post_id))}
            onValidity={(v) => run("validity", () => api.setValidity(t.mattermost_post_id, v))}
          />
        )
      }
    >
      {detail.loading ? (
        <Spinner />
      ) : !t ? (
        <ErrorState message={detail.error ?? "Не найдено"} onRetry={detail.reload} />
      ) : (
        <div className="flex flex-col divide-y divide-line">
          <Field label="Сообщение">
            <p className="whitespace-pre-wrap text-sm leading-relaxed text-muted">
              {t.mattermost_message_text || t.mattermost_message_preview}
            </p>
          </Field>
          <div className="grid grid-cols-2 gap-x-4">
            <Field label="Статус">
              <Badge tone={lifecycleStatus(t).tone} live={lifecycleStatus(t).tone === "live"}>
                {lifecycleStatus(t).label}
              </Badge>
            </Field>
            <Field label="Валидность">
              {t.validity_status ? (
                <Badge tone={validityTone(t.validity_status)}>{t.validity_status}</Badge>
              ) : (
                <span className="text-faint">не задана</span>
              )}
            </Field>
            <Field label="Jira">
              {t.jira_issue_url ? (
                <a
                  href={t.jira_issue_url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 font-mono text-live hover:underline"
                >
                  {t.jira_issue_key} <ExternalLink size={12} />
                </a>
              ) : (
                <span className="text-faint">нет</span>
              )}
            </Field>
            <Field label="Канал">
              <span className="text-muted">{t.mattermost_channel_name || t.mattermost_channel_id}</span>
            </Field>
            <Field label="Подтверждён">{formatDateTime(t.confirmed_at)}</Field>
            <Field label="Закрыт">{formatDateTime(t.resolved_at)}</Field>
            <Field label="Сигнатура">
              <span className="font-mono text-xs text-muted">{t.alert_signature || "—"}</span>
            </Field>
            <Field label="Создан">{formatDateTime(t.mattermost_message_created_at)}</Field>
          </div>
          {t.last_error && (
            <Field label="Последняя ошибка">
              <p className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 font-mono text-xs text-danger">
                {t.last_error}
              </p>
            </Field>
          )}
        </div>
      )}
    </SlideOver>
  );
}

function LifecycleActions({
  ticket,
  busy,
  onConfirm,
  onEnd,
  onPostmortem,
  onSummary,
  onValidity,
}: {
  ticket: AlertTicket;
  busy: string | null;
  onConfirm: () => void;
  onEnd: () => void;
  onPostmortem: () => void;
  onSummary: () => void;
  onValidity: (v: string) => void;
}) {
  const [validityOpen, setValidityOpen] = useState(false);
  return (
    <div className="flex flex-wrap items-center gap-2">
      {!ticket.valid_incident && (
        <Button
          variant="primary"
          loading={busy === "confirm"}
          icon={<CheckCircle2 size={15} />}
          onClick={onConfirm}
        >
          Подтвердить
        </Button>
      )}
      {ticket.valid_incident && ticket.incident_post_id && (
        <Button
          variant="outline"
          loading={busy === "end"}
          icon={<FlagTriangleRight size={15} />}
          onClick={onEnd}
        >
          Завершить
        </Button>
      )}
      <Button
        variant="outline"
        loading={busy === "postmortem"}
        icon={<FileText size={15} />}
        onClick={onPostmortem}
      >
        Постмортем
      </Button>
      <Button
        variant="ghost"
        loading={busy === "summary"}
        icon={<ListChecks size={15} />}
        onClick={onSummary}
      >
        Саммари
      </Button>

      <div className="relative ml-auto">
        <Button variant="outline" loading={busy === "validity"} onClick={() => setValidityOpen((v) => !v)}>
          Валидность
        </Button>
        {validityOpen && (
          <div className="absolute bottom-full right-0 mb-2 flex w-44 flex-col overflow-hidden rounded-lg border border-line2 bg-raised shadow-panel">
            {VALIDITY_OPTIONS.map((v) => (
              <button
                key={v}
                onClick={() => {
                  setValidityOpen(false);
                  onValidity(v);
                }}
                className="px-3 py-2 text-left text-sm text-muted transition-colors hover:bg-white/[0.05] hover:text-fg"
              >
                {v}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
