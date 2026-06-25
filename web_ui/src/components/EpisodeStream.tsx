import { useMemo } from "react";
import type { AlertTicket } from "@/api/types";
import { validityTone, type Tone } from "@/lib/format";

const DOT_COLOR: Record<Tone, string> = {
  valid: "#3FB950",
  falsealarm: "#D29922",
  expected: "#A371F7",
  danger: "#F85149",
  live: "#2F81F7",
  muted: "#5A6472",
};

interface Lane {
  signature: string;
  alerts: AlertTicket[];
  open: boolean; // has an unresolved valid root
  count: number;
}

function toneOf(t: AlertTicket): Tone {
  if (t.creation_status === "failed_jira" || t.confirmation_status === "failed_confirmation")
    return "danger";
  if (t.valid_incident && !t.resolved_at) return "live";
  if (t.validity_label) return validityTone(t.validity_label);
  return "muted";
}

/**
 * EpisodeStream — the dashboard signature. Each row is an alert *signature*
 * (one recurring problem); dots are firings placed on a shared time axis, so a
 * flapping/noisy signature reads as a dense cluster at a glance. The root firing
 * is a ring; repeats are small dots. A live (open, unresolved) episode glows.
 */
export function EpisodeStream({ alerts }: { alerts: AlertTicket[] }) {
  const { lanes, domain } = useMemo(() => {
    const bySig = new Map<string, AlertTicket[]>();
    for (const a of alerts) {
      const key = a.alert_signature || a.mattermost_post_id;
      const list = bySig.get(key);
      if (list) list.push(a);
      else bySig.set(key, [a]);
    }

    const times = alerts
      .map((a) => a.mattermost_message_created_at)
      .filter(Boolean)
      .map((s) => new Date(s as string).getTime())
      .filter((n) => !Number.isNaN(n));
    const min = times.length ? Math.min(...times) : 0;
    const max = times.length ? Math.max(...times) : 1;

    const lanes: Lane[] = [...bySig.entries()].map(([signature, list]) => ({
      signature,
      alerts: list,
      open: list.some((a) => a.valid_incident && !a.resolved_at),
      count: list.length,
    }));

    // Open episodes first, then the busiest signatures.
    lanes.sort((a, b) => Number(b.open) - Number(a.open) || b.count - a.count);

    return { lanes: lanes.slice(0, 9), domain: { min, max: max === min ? min + 1 : max } };
  }, [alerts]);

  if (!lanes.length) {
    return (
      <div className="flex h-32 items-center justify-center text-sm text-faint">
        Нет алертов для потока эпизодов
      </div>
    );
  }

  const pos = (iso: string | null) => {
    if (!iso) return 50;
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return 50;
    return ((t - domain.min) / (domain.max - domain.min)) * 100;
  };

  return (
    <div className="flex flex-col gap-1.5">
      {lanes.map((lane) => (
        <div
          key={lane.signature}
          className={`group grid grid-cols-[minmax(0,180px)_1fr_auto] items-center gap-3 rounded-lg px-2 py-1.5 transition-colors hover:bg-white/[0.03] ${
            lane.open ? "bg-live/[0.04]" : ""
          }`}
        >
          <div className="flex items-center gap-2 truncate">
            {lane.open && (
              <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-live animate-pulse-live" />
            )}
            <span
              className="truncate font-mono text-xs text-muted group-hover:text-fg"
              title={lane.signature}
            >
              {lane.signature}
            </span>
          </div>

          <div className="relative h-6">
            <div className="absolute left-0 right-0 top-1/2 h-px -translate-y-1/2 bg-line" />
            {lane.alerts.map((a) => {
              const tone = toneOf(a);
              const color = DOT_COLOR[tone];
              const live = tone === "live";
              return (
                <span
                  key={a.id}
                  title={`${a.mattermost_alert_title ?? a.mattermost_post_id}\n${a.mattermost_message_created_at ?? ""}`}
                  className="absolute top-1/2 -translate-x-1/2 -translate-y-1/2"
                  style={{ left: `${pos(a.mattermost_message_created_at)}%` }}
                >
                  {a.is_root ? (
                    <span
                      className="block rounded-full"
                      style={{
                        width: 10,
                        height: 10,
                        border: `2px solid ${color}`,
                        background: live ? color : "transparent",
                        boxShadow: live ? `0 0 8px ${color}` : "none",
                      }}
                    />
                  ) : (
                    <span
                      className="block rounded-full"
                      style={{ width: 5, height: 5, background: color, opacity: 0.85 }}
                    />
                  )}
                </span>
              );
            })}
          </div>

          <span className="font-mono text-[11px] tabular-nums text-faint">×{lane.count}</span>
        </div>
      ))}
    </div>
  );
}
