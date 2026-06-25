import { useMemo } from "react";
import type { TimeseriesPoint } from "@/api/types";

/**
 * AreaSignal — the dashboard hero. Renders the daily alert volume as a layered
 * "oscilloscope trace": a faint total band with a brighter confirmed-incident
 * trace on top, over a thin engineering grid. Pure SVG, responsive via
 * viewBox, no chart library.
 */
export function AreaSignal({ points }: { points: TimeseriesPoint[] }) {
  const W = 720;
  const H = 200;
  const pad = { t: 16, r: 8, b: 22, l: 8 };

  const { totalPath, totalArea, confPath, confArea, ticks, max } = useMemo(() => {
    const n = points.length;
    const max = Math.max(1, ...points.map((p) => p.total));
    const iw = W - pad.l - pad.r;
    const ih = H - pad.t - pad.b;
    const x = (i: number) => pad.l + (n <= 1 ? iw / 2 : (i / (n - 1)) * iw);
    const y = (v: number) => pad.t + ih - (v / max) * ih;

    const line = (sel: (p: TimeseriesPoint) => number) =>
      points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(sel(p)).toFixed(1)}`).join(" ");
    const area = (sel: (p: TimeseriesPoint) => number) => {
      if (!n) return "";
      return `${line(sel)} L${x(n - 1).toFixed(1)},${(H - pad.b).toFixed(1)} L${x(0).toFixed(1)},${(
        H - pad.b
      ).toFixed(1)} Z`;
    };

    const tickCount = Math.min(6, n);
    const ticks = Array.from({ length: tickCount }, (_, k) => {
      const i = Math.round((k / Math.max(1, tickCount - 1)) * (n - 1));
      return { x: x(i), label: points[i]?.date.slice(5) ?? "" };
    });

    return {
      totalPath: line((p) => p.total),
      totalArea: area((p) => p.total),
      confPath: line((p) => p.confirmed),
      confArea: area((p) => p.confirmed),
      ticks,
      max,
    };
  }, [points]);

  if (!points.length) {
    return (
      <div className="flex h-[200px] items-center justify-center text-sm text-faint">
        Нет данных за период
      </div>
    );
  }

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" preserveAspectRatio="none" role="img">
      <defs>
        <linearGradient id="totalFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#8B949E" stopOpacity="0.18" />
          <stop offset="100%" stopColor="#8B949E" stopOpacity="0" />
        </linearGradient>
        <linearGradient id="confFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#2F81F7" stopOpacity="0.35" />
          <stop offset="100%" stopColor="#2F81F7" stopOpacity="0" />
        </linearGradient>
      </defs>

      {/* horizontal grid */}
      {[0, 0.5, 1].map((f) => {
        const yy = pad.t + (H - pad.t - pad.b) * f;
        return (
          <line
            key={f}
            x1={pad.l}
            x2={W - pad.r}
            y1={yy}
            y2={yy}
            stroke="rgba(230,237,243,0.06)"
            strokeWidth="1"
          />
        );
      })}
      <text x={pad.l} y={pad.t - 4} className="fill-faint font-mono" fontSize="9">
        {max}
      </text>

      <path d={totalArea} fill="url(#totalFill)" />
      <path d={totalPath} fill="none" stroke="#8B949E" strokeWidth="1.5" strokeOpacity="0.7" />
      <path d={confArea} fill="url(#confFill)" />
      <path d={confPath} fill="none" stroke="#2F81F7" strokeWidth="2" />

      {ticks.map((t, i) => (
        <text
          key={i}
          x={t.x}
          y={H - 6}
          textAnchor="middle"
          className="fill-faint font-mono"
          fontSize="9"
        >
          {t.label}
        </text>
      ))}
    </svg>
  );
}

/** A labeled horizontal magnitude bar used in distribution panels. */
export function DistroBar({
  label,
  value,
  max,
  colorClass,
}: {
  label: string;
  value: number;
  max: number;
  colorClass: string;
}) {
  const pct = max > 0 ? Math.max(2, (value / max) * 100) : 0;
  return (
    <div className="flex items-center gap-3">
      <span className="w-32 shrink-0 truncate text-xs text-muted" title={label}>
        {label}
      </span>
      <div className="h-2 flex-1 overflow-hidden rounded-full bg-white/[0.04]">
        <div className={`h-full rounded-full ${colorClass}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="w-8 shrink-0 text-right font-mono text-xs tabular-nums text-fg">
        {value}
      </span>
    </div>
  );
}
