import { useState } from "react";
import { Activity, KeyRound, Loader2 } from "lucide-react";
import { api, setToken, clearToken } from "@/api/client";
import { useAuth } from "@/auth/TokenContext";

export default function LoginScreen() {
  const { login } = useAuth();
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const token = value.trim();
    if (!token) return;
    setBusy(true);
    setError(null);
    // Stash the token so the probe request carries it, then validate.
    setToken(token);
    try {
      await api.verifyToken();
      login(token);
    } catch {
      clearToken();
      setError("Токен отклонён. Проверьте ADMIN_UI_TOKEN.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden px-6">
      {/* A slow oscilloscope sweep behind the card sets the instrument tone. */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden opacity-[0.35]">
        <div className="absolute left-0 top-1/2 h-px w-full bg-gradient-to-r from-transparent via-live to-transparent animate-sweep" />
      </div>

      <div className="relative w-full max-w-sm animate-fade-up">
        <div className="mb-8 flex flex-col items-center text-center">
          <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-xl border border-line2 bg-raised shadow-glow">
            <Activity size={22} className="text-live" />
          </div>
          <h1 className="text-xl font-semibold tracking-tight">Incident Console</h1>
          <p className="mt-1 font-mono text-xs uppercase tracking-[0.2em] text-faint">
            mattermost · jira bridge
          </p>
        </div>

        <form onSubmit={submit} className="panel p-6">
          <label htmlFor="token" className="eyebrow mb-2 block">
            Admin token
          </label>
          <div className="relative">
            <KeyRound
              size={15}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint"
            />
            <input
              id="token"
              type="password"
              autoFocus
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="Bearer-токен"
              className="w-full rounded-lg border border-line2 bg-ink py-2.5 pl-9 pr-3 font-mono text-sm text-fg placeholder:text-faint focus:border-live focus:outline-none"
            />
          </div>

          {error && <p className="mt-3 text-sm text-danger">{error}</p>}

          <button
            type="submit"
            disabled={busy || !value.trim()}
            className="mt-5 flex w-full items-center justify-center gap-2 rounded-lg bg-live py-2.5 text-sm font-semibold text-white transition-colors hover:bg-live/90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy && <Loader2 size={15} className="animate-spin" />}
            Войти
          </button>
        </form>

        <p className="mt-4 text-center text-xs leading-relaxed text-faint">
          Доступ только за reverse-proxy / firewall. Токен хранится в
          localStorage этого браузера.
        </p>
      </div>
    </div>
  );
}
