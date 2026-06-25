import { Activity, AlertTriangle, CircleDot, Clock, Timer, Hash } from "lucide-react";
import type { ReactNode } from "react";
import { api } from "@/api/client";
import { useApi } from "@/lib/useApi";
import { formatDuration } from "@/lib/format";
import type { Tone } from "@/lib/format";
import { Spinner, ErrorState } from "@/components/ui";
import { AreaSignal, DistroBar } from "@/components/charts";
import { EpisodeStream } from "@/components/EpisodeStream";

const TONE_TEXT: Record<Tone, string> = {
  valid: "text-valid",
  falsealarm: "text-falsealarm",
  expected: "text-expected",
  danger: "text-danger",
  live: "text-live",
  muted: "text-fg",
};

function Readout({
  label,
  value,
  sub,
  tone = "muted",
  icon,
  glow = false,
}: {
  label: string;
  value: ReactNode;
  sub?: string;
  tone?: Tone;
  icon: ReactNode;
  glow?: boolean;
}) {
  return (
    <div className={`panel relative overflow-hidden p-4 ${glow ? "shadow-glow" : ""}`}>
      <div className="flex items-center justify-between">
        <span className="eyebrow">{label}</span>
        <span className="text-faint">{icon}</span>
      </div>
      <div className={`mt-3 font-mono text-readout tabular-nums ${TONE_TEXT[tone]}`}>{value}</div>
      {sub && <div className="mt-1 text-xs text-faint">{sub}</div>}
    </div>
  );
}

const CREATION_LABELS: Record<string, string> = {
  pending_jira: "Ожидает Jira",
  jira_created: "Jira создана",
  failed_jira: "Ошибка Jira",
};
const VALIDITY_COLORS: Record<string, string> = {
  Валидный: "bg-valid",
  Ложный: "bg-falsealarm",
  Ожидаемый: "bg-expected",
  "Не заполнено": "bg-faint",
};

export default function Dashboard() {
  const stats = useApi(() => api.stats(), []);
  const alerts = useApi(() => api.alerts({ limit: 200 }), []);

  if (stats.loading) return <Spinner label="Загрузка статистики…" />;
  if (stats.error || !stats.data)
    return <ErrorState message={stats.error ?? "Нет данных"} onRetry={stats.reload} />;

  const s = stats.data;
  const validityMax = Math.max(1, ...Object.values(s.by_validity_label));

  return (
    <div className="flex flex-col gap-6">
      {/* Instrument readouts */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        <Readout
          label="Открытые"
          value={s.open}
          sub="активные инциденты"
          tone="live"
          glow={s.open > 0}
          icon={<Activity size={15} />}
        />
        <Readout
          label="MTTA"
          value={formatDuration(s.mtta_seconds)}
          sub="ср. до подтверждения"
          icon={<Clock size={15} />}
        />
        <Readout
          label="MTTR"
          value={formatDuration(s.mttr_seconds)}
          sub="ср. до закрытия"
          icon={<Timer size={15} />}
        />
        <Readout
          label="Без Jira"
          value={s.pending_jira}
          sub="не заведены"
          tone={s.pending_jira > 0 ? "falsealarm" : "muted"}
          icon={<CircleDot size={15} />}
        />
        <Readout
          label="Ошибки"
          value={s.failed}
          sub="требуют внимания"
          tone={s.failed > 0 ? "danger" : "muted"}
          icon={<AlertTriangle size={15} />}
        />
      </div>

      {/* Signal hero */}
      <section className="panel p-5">
        <div className="mb-2 flex items-end justify-between">
          <div>
            <h2 className="text-sm font-semibold tracking-tight">Поток алертов</h2>
            <p className="eyebrow mt-0.5">последние {s.timeseries_days} дней</p>
          </div>
          <div className="flex items-center gap-4 font-mono text-[11px] text-faint">
            <span className="flex items-center gap-1.5">
              <span className="h-2 w-2 rounded-full bg-muted/70" /> всего
            </span>
            <span className="flex items-center gap-1.5">
              <span className="h-2 w-2 rounded-full bg-live" /> подтв.
            </span>
          </div>
        </div>
        <AreaSignal points={s.timeseries_daily} />
      </section>

      {/* Episode stream */}
      <section className="panel p-5">
        <div className="mb-3 flex items-end justify-between">
          <div>
            <h2 className="text-sm font-semibold tracking-tight">Поток эпизодов</h2>
            <p className="eyebrow mt-0.5">по сигнатуре · root + повторы</p>
          </div>
          <span className="font-mono text-[11px] text-faint">○ root · • повтор</span>
        </div>
        {alerts.loading ? (
          <Spinner />
        ) : alerts.data ? (
          <EpisodeStream alerts={alerts.data.alerts} />
        ) : (
          <ErrorState message="Не удалось загрузить алерты" onRetry={alerts.reload} />
        )}
      </section>

      {/* Distributions + channels */}
      <div className="grid gap-6 lg:grid-cols-3">
        <section className="panel p-5">
          <h2 className="mb-4 text-sm font-semibold tracking-tight">Валидность</h2>
          <div className="flex flex-col gap-3">
            {Object.entries(s.by_validity_label).map(([label, count]) => (
              <DistroBar
                key={label}
                label={label}
                value={count}
                max={validityMax}
                colorClass={VALIDITY_COLORS[label] ?? "bg-faint"}
              />
            ))}
            {!Object.keys(s.by_validity_label).length && (
              <p className="text-xs text-faint">Нет данных</p>
            )}
          </div>
        </section>

        <section className="panel p-5">
          <h2 className="mb-4 text-sm font-semibold tracking-tight">Создание Jira</h2>
          <div className="flex flex-col gap-3">
            {Object.entries(s.by_creation_status).map(([label, count]) => (
              <DistroBar
                key={label}
                label={CREATION_LABELS[label] ?? label}
                value={count}
                max={Math.max(1, ...Object.values(s.by_creation_status))}
                colorClass={label === "failed_jira" ? "bg-danger" : "bg-live"}
              />
            ))}
          </div>
        </section>

        <section className="panel p-5">
          <h2 className="mb-4 text-sm font-semibold tracking-tight">Топ каналов</h2>
          <div className="flex flex-col gap-2.5">
            {s.top_channels.slice(0, 7).map((c) => (
              <div key={c.channel_id} className="flex items-center gap-2 text-sm">
                <Hash size={13} className="shrink-0 text-faint" />
                <span className="truncate text-muted" title={c.channel_id}>
                  {c.channel_name || c.channel_id}
                </span>
                <span className="ml-auto font-mono text-xs tabular-nums text-fg">{c.count}</span>
              </div>
            ))}
            {!s.top_channels.length && <p className="text-xs text-faint">Нет данных</p>}
          </div>
        </section>
      </div>
    </div>
  );
}
