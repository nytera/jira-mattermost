import { useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import type { ReactNode } from "react";
import {
  Activity,
  LayoutDashboard,
  Siren,
  Bell,
  SlidersHorizontal,
  ScrollText,
  LogOut,
  Menu,
  X,
} from "lucide-react";
import { useAuth } from "@/auth/TokenContext";

const NAV = [
  { to: "/", label: "Дашборд", icon: LayoutDashboard, end: true },
  { to: "/incidents", label: "Инциденты", icon: Siren, end: false },
  { to: "/alerts", label: "Алерты", icon: Bell, end: false },
  { to: "/settings", label: "Настройки", icon: SlidersHorizontal, end: false },
  { to: "/logs", label: "Логи", icon: ScrollText, end: false },
];

function NavItems({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav className="flex flex-col gap-1">
      {NAV.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          onClick={onNavigate}
          className={({ isActive }) =>
            `group flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${
              isActive
                ? "bg-live/10 text-fg shadow-[inset_2px_0_0_0_theme(colors.live)]"
                : "text-muted hover:bg-white/[0.04] hover:text-fg"
            }`
          }
        >
          {({ isActive }) => (
            <>
              <Icon
                size={17}
                className={isActive ? "text-live" : "text-faint group-hover:text-muted"}
              />
              {label}
            </>
          )}
        </NavLink>
      ))}
    </nav>
  );
}

function Brand() {
  return (
    <div className="flex items-center gap-2.5 px-2">
      <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-line2 bg-raised">
        <Activity size={16} className="text-live" />
      </div>
      <div className="leading-tight">
        <div className="text-sm font-semibold tracking-tight">Incident Console</div>
        <div className="font-mono text-[10px] uppercase tracking-[0.18em] text-faint">
          mm · jira
        </div>
      </div>
    </div>
  );
}

export default function Layout({ children }: { children: ReactNode }) {
  const { logout } = useAuth();
  const [open, setOpen] = useState(false);
  const location = useLocation();
  const current = NAV.find((n) => (n.end ? location.pathname === n.to : location.pathname.startsWith(n.to) && n.to !== "/"));
  const title = current?.label ?? "Дашборд";

  return (
    <div className="flex min-h-screen">
      {/* Desktop sidebar */}
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col justify-between border-r border-line bg-panel/60 px-3 py-5 backdrop-blur md:flex">
        <div className="flex flex-col gap-6">
          <Brand />
          <NavItems />
        </div>
        <button
          onClick={logout}
          className="flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted transition-colors hover:bg-white/[0.04] hover:text-danger"
        >
          <LogOut size={17} className="text-faint" />
          Выйти
        </button>
      </aside>

      {/* Mobile drawer */}
      {open && (
        <div className="fixed inset-0 z-40 md:hidden">
          <div className="absolute inset-0 bg-black/60" onClick={() => setOpen(false)} />
          <aside className="absolute left-0 top-0 flex h-full w-64 flex-col justify-between border-r border-line bg-panel px-3 py-5">
            <div className="flex flex-col gap-6">
              <Brand />
              <NavItems onNavigate={() => setOpen(false)} />
            </div>
            <button
              onClick={logout}
              className="flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted hover:text-danger"
            >
              <LogOut size={17} /> Выйти
            </button>
          </aside>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex items-center gap-3 border-b border-line bg-ink/80 px-5 py-3.5 backdrop-blur">
          <button
            className="md:hidden text-muted hover:text-fg"
            onClick={() => setOpen((v) => !v)}
            aria-label="Меню"
          >
            {open ? <X size={20} /> : <Menu size={20} />}
          </button>
          <h1 className="text-base font-semibold tracking-tight">{title}</h1>
          <span className="ml-auto flex items-center gap-2 font-mono text-[11px] uppercase tracking-wider text-faint">
            <span className="h-1.5 w-1.5 rounded-full bg-valid animate-pulse-live" />
            live
          </span>
        </header>
        <main className="mx-auto w-full max-w-[1240px] flex-1 px-5 py-6">{children}</main>
      </div>
    </div>
  );
}
