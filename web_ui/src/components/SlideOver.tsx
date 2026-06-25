import { useEffect } from "react";
import type { ReactNode } from "react";
import { X } from "lucide-react";

export function SlideOver({
  open,
  onClose,
  title,
  subtitle,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  subtitle?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-40">
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-sm animate-fade-up"
        onClick={onClose}
      />
      <div className="absolute right-0 top-0 flex h-full w-full max-w-xl flex-col border-l border-line bg-panel shadow-panel">
        <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
          <div className="min-w-0">
            <div className="truncate text-base font-semibold tracking-tight">{title}</div>
            {subtitle && <div className="mt-0.5 truncate text-sm text-muted">{subtitle}</div>}
          </div>
          <button
            onClick={onClose}
            className="shrink-0 rounded-lg p-1.5 text-faint transition-colors hover:bg-white/[0.05] hover:text-fg"
            aria-label="Закрыть"
          >
            <X size={18} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && <div className="border-t border-line px-5 py-3">{footer}</div>}
      </div>
    </div>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1 py-2">
      <span className="eyebrow">{label}</span>
      <div className="text-sm text-fg">{children}</div>
    </div>
  );
}
