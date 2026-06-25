import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { CheckCircle2, Info, XCircle, X } from "lucide-react";

type ToastTone = "ok" | "error" | "info";

interface Toast {
  id: number;
  tone: ToastTone;
  message: string;
}

interface ToastApi {
  push: (message: string, tone?: ToastTone) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const TONE_STYLES: Record<ToastTone, { icon: ReactNode; ring: string }> = {
  ok: { icon: <CheckCircle2 size={16} className="text-valid" />, ring: "border-valid/40" },
  error: { icon: <XCircle size={16} className="text-danger" />, ring: "border-danger/40" },
  info: { icon: <Info size={16} className="text-live" />, ring: "border-live/40" },
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const counter = useRef(0);

  const remove = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const push = useCallback(
    (message: string, tone: ToastTone = "info") => {
      const id = ++counter.current;
      setToasts((prev) => [...prev, { id, tone, message }]);
      window.setTimeout(() => remove(id), 5200);
    },
    [remove],
  );

  const value = useMemo<ToastApi>(() => ({ push }), [push]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed bottom-5 right-5 z-50 flex w-[min(420px,calc(100vw-2.5rem))] flex-col gap-2">
        {toasts.map((t) => (
          <div
            key={t.id}
            role="status"
            className={`pointer-events-auto flex items-start gap-3 rounded-lg border ${TONE_STYLES[t.tone].ring} bg-raised/95 px-4 py-3 shadow-panel backdrop-blur animate-fade-up`}
          >
            <span className="mt-0.5 shrink-0">{TONE_STYLES[t.tone].icon}</span>
            <p className="flex-1 text-sm leading-snug text-fg">{t.message}</p>
            <button
              onClick={() => remove(t.id)}
              className="shrink-0 text-faint transition-colors hover:text-fg"
              aria-label="Закрыть"
            >
              <X size={15} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}
