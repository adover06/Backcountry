import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api } from "../api/client";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  const refreshMe = useCallback(async () => {
    try {
      const me = await api.get("/api/auth/me");
      setUser(me);
    } catch {
      setUser(null);
    }
  }, []);

  useEffect(() => {
    (async () => {
      await refreshMe();
      setLoading(false);
    })();
  }, [refreshMe]);

  const login = useCallback(async (email, password) => {
    const me = await api.post("/api/auth/login", { email, password });
    setUser(me);
    return me;
  }, []);

  const register = useCallback(async (email, password, display_name) => {
    const me = await api.post("/api/auth/register", { email, password, display_name });
    setUser(me);
    return me;
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.post("/api/auth/logout");
    } finally {
      setUser(null);
    }
  }, []);

  const updateProfile = useCallback(async (patch) => {
    const me = await api.patch("/api/auth/me", patch);
    setUser(me);
    return me;
  }, []);

  return (
    <AuthContext.Provider
      value={{ user, loading, login, register, logout, refreshMe, updateProfile }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
