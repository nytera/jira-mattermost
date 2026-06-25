import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Loader2 } from "lucide-react";
import type { Tone } from "@/lib/format";

const TONE_TEXT: Record<Tone, string> = {
  valid: "text-valid",
  falsealarm: "text-falsealarm",
  expected: "text-expected",
  danger: "text-danger",
  live: "text-live",
  muted: "text-muted",
};

const TONE_DOT: Record<Tone, string> = {
  valid: "bg-valid",
  falsealarm: "bg-falsealarm",
  expected: "bg-expected",
  danger: "bg-danger",
  live: "bg-live",
  muted: "bg-faint",
};

const TONE_CHIP: Record<Tone, string> = {
  valid: "border-valid/30 bg-valid/10 text-valid",
  falsealarm: "border-falsealarm/30 bg-falsealarm/10 text-falsealarm",
  expected: "border-expected/30 bg-expected/10 text-expected",
  danger: "border-danger/30 bg-danger/10 text-danger",
  live: "border-live/40 bg-live/10 text-live",
  muted: "border-line2 bg-white/[0.03] text-muted",
};

export function StatusDot({ tone, live = false }: { tone: Tone; live?: boolean }) {
  return (
    <span className="relative inline-flex h-2 w-2 shrink-0">
      {live && (
        <span
          className={`absolute inline-flex h-full w-full rounded-full ${TONE_DOT[tone]} opacity-60 animate-pulse-live`}
        />
      )}
      <span className={`relative inline-flex h-2 w-2 rounded-full ${TONE_DOT[tone]}`} />
    </span>
  );
}

export function Badge({
  tone = "muted",
  children,
  live = false,
}: {
  tone?: Tone;
  children: ReactNode;
  live?: boolean;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 font-mono text-[11px] uppercase tracking-wider ${TONE_CHIP[tone]}`}
    >
      <StatusDot tone={tone} live={live} />
      {children}
    </span>
  );
}

export function ToneText({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={TONE_TEXT[tone]}>{children}</span>;
}

type Variant = "primary" | "ghost" | "danger" | "outline";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-live text-white hover:bg-live/90 border-transparent",
  outline: "border-line2 bg-transparent text-fg hover:bg-white/[0.04]",
  ghost: "border-transparent bg-transparent text-muted hover:text-fg hover:bg-white/[0.04]",
  danger: "border-danger/40 bg-danger/10 text-danger hover:bg-danger/20",
};

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  loading?: boolean;
  icon?: ReactNode;
}

export function Button({
  variant = "outline",
  loading = false,
  icon,
  children,
  className = "",
  disabled,
  ...rest
}: ButtonProps) {
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      className={`inline-flex items-center justify-center gap-2 rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${VARIANTS[variant]} ${className}`}
    >
      {loading ? <Loader2 size={15} className="animate-spin" /> : icon}
      {children}
    </button>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-3 py-16 text-muted">
      <Loader2 size={18} className="animate-spin" />
      {label && <span className="text-sm">{label}</span>}
    </div>
  );
}

export function EmptyState({ icon, title, hint }: { icon?: ReactNode; title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-16 text-center">
      {icon && <div className="text-faint">{icon}</div>}
      <p className="text-sm font-medium text-muted">{title}</p>
      {hint && <p className="max-w-sm text-xs text-faint">{hint}</p>}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
      <p className="text-sm text-danger">{message}</p>
      {onRetry && (
        <Button variant="outline" onClick={onRetry}>
          Повторить
        </Button>
      )}
    </div>
  );
}

export function SectionHeader({
  title,
  caption,
  action,
}: {
  title: string;
  caption?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex items-end justify-between gap-4">
      <div>
        <h2 className="text-lg font-semibold tracking-tight text-fg">{title}</h2>
        {caption && <p className="mt-0.5 text-sm text-muted">{caption}</p>}
      </div>
      {action}
    </div>
  );
}
