import { useState } from "react";
import { Link2, Plus, RefreshCw, Siren, ExternalLink } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { useApi } from "@/lib/useApi";
import { useToast } from "@/components/Toast";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  SectionHeader,
  Spinner,
} from "@/components/ui";
import { lifecycleStatus, validityTone } from "@/lib/format";

export default function Alerts() {
  const toast = useToast();
  const list = useApi(() => api.alerts({ limit: 200 }), []);

  const [link, setLink] = useState("");
  const [creating, setCreating] = useState(false);
  const [rowBusy, setRowBusy] = useState<string | null>(null);

  const rows = list.data?.alerts ?? [];

  async function createFromLink() {
    const value = link.trim();
    if (!value || creating) return;
    setCreating(true);
    try {
      const res = await api.createFromLink(value);
      toast.push(res.message, res.ok ? "ok" : "error");
      if (res.ok) {
        setLink("");
        list.reload();
      }
    } catch (e) {
      toast.push(e instanceof ApiError ? e.message : "Ошибка создания задачи", "error");
    } finally {
      setCreating(false);
    }
  }

  async function recreate(postId: string, force: boolean) {
    if (force && !window.confirm("Пересоздать Jira-задачу? Текущая будет отвязана.")) return;
    setRowBusy(postId);
    try {
      const res = await api.recreate(postId, force);
      toast.push(res.message, res.ok ? "ok" : "error");
      list.reload();
    } catch (e) {
      toast.push(e instanceof ApiError ? e.message : "Ошибка операции", "error");
    } finally {
      setRowBusy(null);
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <SectionHeader
        title="Алерты"
        caption="Сырые тикеты алертов. Создание и пересоздание Jira-задач."
      />

      <div className="panel p-4">
        <p className="eyebrow mb-2">Создать из ссылки</p>
        <div className="flex flex-col gap-2 sm:flex-row">
          <div className="relative flex-1">
            <Link2
              size={15}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint"
            />
            <input
              value={link}
              onChange={(e) => setLink(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") createFromLink();
              }}
              placeholder="Ссылка на пост Mattermost или post id"
              className="w-full rounded-lg border border-line2 bg-ink py-1.5 pl-9 pr-3 text-sm text-fg placeholder:text-faint focus:border-live/40 focus:outline-none"
            />
          </div>
          <Button
            variant="primary"
            icon={<Plus size={15} />}
            loading={creating}
            disabled={!link.trim()}
            onClick={createFromLink}
          >
            Создать задачу
          </Button>
        </div>
      </div>

      <div className="panel overflow-hidden">
        {list.loading ? (
          <Spinner label="Загрузка алертов…" />
        ) : list.error ? (
          <ErrorState message={list.error} onRetry={list.reload} />
        ) : rows.length === 0 ? (
          <EmptyState icon={<Siren size={28} />} title="Нет алертов" hint="Пока не зафиксировано ни одного алерта." />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left">
                <th className="px-4 py-2.5 eyebrow font-normal">Алерт</th>
                <th className="px-4 py-2.5 eyebrow font-normal">Статус</th>
                <th className="hidden px-4 py-2.5 eyebrow font-normal md:table-cell">Валидность</th>
                <th className="hidden px-4 py-2.5 eyebrow font-normal lg:table-cell">Jira</th>
                <th className="px-4 py-2.5 eyebrow font-normal text-right">Действия</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => {
                const st = lifecycleStatus(t);
                const postId = t.mattermost_post_id;
                const busy = rowBusy === postId;
                return (
                  <tr
                    key={t.id}
                    className="border-b border-line/60 transition-colors last:border-0 hover:bg-white/[0.03]"
                  >
                    <td className="max-w-0 px-4 py-3">
                      <div className="truncate font-medium text-fg">
                        {t.mattermost_alert_title || t.mattermost_message_preview || "—"}
                      </div>
                      <div className="truncate font-mono text-[11px] text-faint">{postId}</div>
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
                        t.jira_issue_url ? (
                          <a
                            href={t.jira_issue_url}
                            target="_blank"
                            rel="noreferrer"
                            className="inline-flex items-center gap-1 text-live hover:underline"
                          >
                            {t.jira_issue_key} <ExternalLink size={12} />
                          </a>
                        ) : (
                          <span className="text-muted">{t.jira_issue_key}</span>
                        )
                      ) : (
                        <span className="text-faint">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex justify-end">
                        {t.jira_issue_key ? (
                          <Button
                            variant="ghost"
                            loading={busy}
                            icon={<RefreshCw size={14} />}
                            onClick={() => recreate(postId, true)}
                          >
                            Пересоздать
                          </Button>
                        ) : (
                          <Button
                            variant="outline"
                            loading={busy}
                            icon={<Plus size={14} />}
                            onClick={() => recreate(postId, false)}
                          >
                            Создать
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
