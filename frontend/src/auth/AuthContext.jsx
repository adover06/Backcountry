import {
  onAuthStateChanged,
  signInWithPopup,
  signOut,
} from "firebase/auth";
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api } from "../api/client";
import { auth, firebaseEnabled, googleProvider } from "./firebase";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [firebaseUser, setFirebaseUser] = useState(null);
  // appUser is our DB row (with `is_admin` flag); null while signed out or pre-invite.
  const [appUser, setAppUser] = useState(null);
  // True when Firebase has a user but our backend returns 403 — they need a code.
  const [needsInvite, setNeedsInvite] = useState(false);
  const [loading, setLoading] = useState(true);

  const fetchAppUser = useCallback(async () => {
    try {
      const me = await api.get("/api/auth/me");
      setAppUser(me);
      setNeedsInvite(false);
    } catch (err) {
      if (err.status === 403) {
        setAppUser(null);
        setNeedsInvite(true);
      } else if (err.status === 401) {
        setAppUser(null);
        setNeedsInvite(false);
      } else {
        setAppUser(null);
      }
    }
  }, []);

  useEffect(() => {
    // No Firebase configured: settle immediately as signed out. Discovery is
    // public, so the app is fully usable in this state.
    if (!firebaseEnabled || !auth) {
      setLoading(false);
      return undefined;
    }
    const unsub = onAuthStateChanged(auth, async (u) => {
      setFirebaseUser(u);
      if (u) {
        await fetchAppUser();
      } else {
        setAppUser(null);
        setNeedsInvite(false);
      }
      setLoading(false);
    });
    return () => unsub();
  }, [fetchAppUser]);

  const signInGoogle = useCallback(async () => {
    if (!auth || !googleProvider) {
      throw new Error("Sign-in is unavailable: Firebase is not configured.");
    }
    await signInWithPopup(auth, googleProvider);
    // onAuthStateChanged will fire and populate appUser / needsInvite.
  }, []);

  const redeemInvite = useCallback(async (code) => {
    const me = await api.post("/api/auth/signup", { invite_code: code.trim() });
    setAppUser(me);
    setNeedsInvite(false);
    return me;
  }, []);

  const logout = useCallback(async () => {
    if (auth) await signOut(auth);
  }, []);

  const updateProfile = useCallback(async (patch) => {
    const me = await api.patch("/api/auth/me", patch);
    setAppUser(me);
    return me;
  }, []);

  return (
    <AuthContext.Provider
      value={{
        firebaseUser,
        user: appUser, // alias kept so existing components keep working
        appUser,
        needsInvite,
        loading,
        firebaseEnabled,
        signInGoogle,
        redeemInvite,
        logout,
        updateProfile,
      }}
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
