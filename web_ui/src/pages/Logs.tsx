import { useMemo, useState } from "react";
import { RefreshCw, ScrollText } from "lucide-react";
import { api } from "@/api/client";
import { useApi } from "@/lib/useApi";
import type { LogRecord } from "@/api/types";
import { Button, EmptyState, ErrorState, SectionHeader, Spinner } from "@/components/ui";
import { formatDateTime } from "@/lib/format";

const LEVELS = ["ВСЕ", "DEBUG", "INFO", "WARNING", "ERROR"];

const LEVEL_TEXT: Record<string, string> = {
  ERROR: "text-danger",
  WARNING: "text-falsealarm",
  INFO: "text-muted",
  DEBUG: "text-faint",
};

/** Coerce an unknown record value to a trimmed string, or "" if absent. */
function field(record: LogRecord, key: string): string {
  const value = record[key];
  if (value == null) return "";
  return String(value);
}

export default function Logs() {
  const [level, setLevel] = useState("ВСЕ");
  const [text, setText] = useState("");
  const logs = useApi(
    () => api.logs({ limit: 500, level: level === "ВСЕ" ? undefined : level }),
    [level],
  );

  const rows = useMemo(() => {
    const all = logs.data?.logs ?? [];
    const needle = text.trim().toLowerCase();
    if (!needle) return all;
    return all.filter((record) => {
      const haystack = [
        field(record, "message"),
        field(record, "event"),
        field(record, "logger"),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(needle);
    });
  }, [logs.data, text]);

  return (
    <div className="flex flex-col gap-5">
      <SectionHeader
        title="Логи"
        caption="Кольцевой буфер в памяти — последние записи журнала процесса."
        action={
          <Button variant="outline" icon={<RefreshCw size={15} />} onClick={logs.reload}>
            Обновить
          </Button>
        }
      />

      <div className="flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap gap-1.5">
          {LEVELS.map((lvl) => (
            <button
              key={lvl}
              onClick={() => setLevel(lvl)}
              className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors ${
                level === lvl
                  ? "border-live/40 bg-live/10 text-live"
                  : "border-line2 text-muted hover:text-fg"
              }`}
            >
              {lvl}
            </button>
          ))}
        </div>
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Фильтр по тексту…"
          className="ml-auto w-full max-w-xs rounded-lg border border-line2 bg-ink px-3 py-1.5 text-sm text-fg placeholder:text-faint focus:border-live/40 focus:outline-none"
        />
      </div>

      {logs.loading ? (
        <div className="panel">
          <Spinner label="Загрузка логов…" />
        </div>
      ) : logs.error ? (
        <div className="panel">
          <ErrorState message={logs.error} onRetry={logs.reload} />
        </div>
      ) : !logs.data?.available ? (
        <div className="panel">
          <EmptyState
            icon={<ScrollText size={28} />}
            title="Лог-буфер недоступен"
            hint="LOG_BUFFER не сконфигурирован — кольцевой буфер в памяти не собирает записи."
          />
        </div>
      ) : rows.length === 0 ? (
        <div className="panel">
          <EmptyState
            icon={<ScrollText size={28} />}
            title="Нет записей"
            hint="Под текущий фильтр уровня и текста ничего не найдено."
          />
        </div>
      ) : (
        <div className="panel max-h-[70vh] overflow-y-auto">
          <ul className="divide-y divide-line/60 font-mono text-xs">
            {rows.map((record, i) => {
              const lvl = field(record, "level").toUpperCase();
              const ts = field(record, "timestamp");
              const logger = field(record, "logger");
              const body = field(record, "message") || field(record, "event");
              return (
                <li
                  key={i}
                  className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-4 py-2"
                >
                  <span
                    className={`shrink-0 uppercase tracking-wider ${LEVEL_TEXT[lvl] ?? "text-faint"}`}
                  >
                    {lvl || "—"}
                  </span>
                  {ts && (
                    <span className="shrink-0 text-faint">
                      {formatDateTime(ts)}
                    </span>
                  )}
                  {logger && <span className="shrink-0 text-faint">{logger}</span>}
                  <span className="min-w-0 flex-1 whitespace-pre-wrap break-words text-fg">
                    {body || "—"}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
