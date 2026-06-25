import { useEffect, useState } from "react";
import { RotateCcw, Save, SlidersHorizontal } from "lucide-react";
import { api, ApiError } from "@/api/client";
import { useApi } from "@/lib/useApi";
import { useToast } from "@/components/Toast";
import type { PromptSetting } from "@/api/types";
import type { Tone } from "@/lib/format";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  SectionHeader,
  Spinner,
} from "@/components/ui";

const SOURCE_META: Record<PromptSetting["source"], { tone: Tone; label: string }> = {
  db: { tone: "live", label: "переопределён" },
  env: { tone: "expected", label: "из env" },
  default: { tone: "muted", label: "по умолчанию" },
};

export default function Settings() {
  const toast = useToast();
  const { data, error, loading, reload } = useApi(() => api.settings(), []);

  // Local edit buffer, keyed by prompt key, seeded from the fetched values.
  const [edited, setEdited] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    if (!data) return;
    setEdited(Object.fromEntries(data.prompts.map((p) => [p.key, p.value])));
  }, [data]);

  function applyResponse(prompts: PromptSetting[]) {
    setEdited(Object.fromEntries(prompts.map((p) => [p.key, p.value])));
  }

  async function save(prompt: PromptSetting) {
    setBusy(prompt.key);
    try {
      const res = await api.saveSetting(prompt.key, edited[prompt.key] ?? prompt.value);
      applyResponse(res.prompts);
      toast.push(`Шаблон «${prompt.label}» сохранён`, "ok");
    } catch (e) {
      toast.push(e instanceof ApiError ? e.message : "Не удалось сохранить шаблон", "error");
    } finally {
      setBusy(null);
    }
  }

  async function reset(prompt: PromptSetting) {
    setBusy(prompt.key);
    try {
      const res = await api.resetSetting(prompt.key);
      applyResponse(res.prompts);
      toast.push(`Шаблон «${prompt.label}» сброшен`, "ok");
    } catch (e) {
      toast.push(e instanceof ApiError ? e.message : "Не удалось сбросить шаблон", "error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <SectionHeader
        title="Настройки"
        caption="Переопределение LLM-промптов на лету: саммари треда и постмортем."
      />

      {loading ? (
        <Spinner label="Загрузка настроек…" />
      ) : error ? (
        <ErrorState message={error} onRetry={reload} />
      ) : !data || data.prompts.length === 0 ? (
        <div className="panel">
          <EmptyState
            icon={<SlidersHorizontal size={28} />}
            title="Нет редактируемых промптов"
            hint="Настраиваемые шаблоны не объявлены."
          />
        </div>
      ) : (
        <div className="flex flex-col gap-5">
          {data.prompts.map((prompt) => {
            const meta = SOURCE_META[prompt.source];
            const current = edited[prompt.key] ?? prompt.value;
            const isBusy = busy === prompt.key;
            return (
              <div key={prompt.key} className="panel flex flex-col gap-3 p-5">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex flex-col">
                    <span className="font-semibold text-fg">{prompt.label}</span>
                    <span className="font-mono text-[11px] text-faint">{prompt.key}</span>
                  </div>
                  <Badge tone={meta.tone} live={prompt.source === "db"}>
                    {meta.label}
                  </Badge>
                </div>

                <textarea
                  value={current}
                  onChange={(e) =>
                    setEdited((prev) => ({ ...prev, [prompt.key]: e.target.value }))
                  }
                  spellCheck={false}
                  className="min-h-[180px] w-full resize-y rounded-lg border border-line2 bg-ink px-3 py-2.5 font-mono text-xs leading-relaxed text-fg outline-none transition-colors focus:border-live/50"
                />

                <div className="flex items-center justify-end gap-2">
                  <Button
                    variant="ghost"
                    icon={<RotateCcw size={15} />}
                    loading={isBusy}
                    disabled={prompt.source === "default"}
                    onClick={() => reset(prompt)}
                  >
                    Сбросить
                  </Button>
                  <Button
                    variant="primary"
                    icon={<Save size={15} />}
                    loading={isBusy}
                    disabled={current === prompt.value}
                    onClick={() => save(prompt)}
                  >
                    Сохранить
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
