import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { clearToken, getToken, setToken, setUnauthorizedHandler } from "@/api/client";

interface AuthState {
  token: string | null;
  authed: boolean;
  login: (token: string) => void;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(() => getToken());

  const logout = useCallback(() => {
    clearToken();
    setTokenState(null);
  }, []);

  const login = useCallback((value: string) => {
    setToken(value);
    setTokenState(value);
  }, []);

  // Any API call that 401s drops us back to the login screen.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      clearToken();
      setTokenState(null);
    });
  }, []);

  const value = useMemo<AuthState>(
    () => ({ token, authed: Boolean(token), login, logout }),
    [token, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
