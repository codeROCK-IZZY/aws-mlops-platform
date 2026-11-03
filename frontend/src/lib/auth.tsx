import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, onUnauthenticated, refreshSession, setAccessToken } from "../api/client";
import type { Role, User } from "../api/types";

const RANK: Record<Role, number> = { viewer: 1, engineer: 2, approver: 3, admin: 4 };

interface AuthState {
  user: User | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  can: (role: Role) => boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  // On first load, try to restore the session from the refresh cookie.
  useEffect(() => {
    onUnauthenticated(() => {
      setAccessToken(null);
      setUser(null);
    });
    refreshSession()
      .then(async (ok) => (ok ? setUser(await api<User>("/auth/me")) : null))
      .catch(() => setUser(null))
      .finally(() => setLoading(false));
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const res = await api<{ access_token: string }>("/auth/login", { body: { email, password } });
    setAccessToken(res.access_token);
    setUser(await api<User>("/auth/me"));
  }, []);

  const logout = useCallback(async () => {
    await api("/auth/logout", { method: "POST" }).catch(() => undefined);
    setAccessToken(null);
    setUser(null);
  }, []);

  const can = useCallback((role: Role) => !!user && RANK[user.role] >= RANK[role], [user]);

  return <AuthContext.Provider value={{ user, loading, login, logout, can }}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
