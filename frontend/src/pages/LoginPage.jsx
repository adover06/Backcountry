import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

export default function LoginPage() {
  const { firebaseUser, appUser, needsInvite, signInGoogle, redeemInvite, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = location.state?.from || "/";

  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  // Already fully signed in → bounce.
  if (appUser) {
    navigate(from, { replace: true });
    return null;
  }

  async function onGoogle() {
    setError(null);
    setBusy(true);
    try {
      await signInGoogle();
    } catch (err) {
      setError(err.message || "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  async function onRedeem(e) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await redeemInvite(code);
      navigate(from, { replace: true });
    } catch (err) {
      setError(err.message || "Invalid invite code");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-950 text-slate-100 p-6">
      <div className="w-full max-w-sm space-y-4 rounded-2xl border border-slate-800 bg-slate-900/60 p-6 shadow-xl">
        <h1 className="text-xl font-semibold">Backcountry</h1>

        {!firebaseUser && !needsInvite && (
          <>
            <p className="text-sm text-slate-400">
              Invite-only beta. Sign in with Google, then enter your invite code.
            </p>
            <button
              onClick={onGoogle}
              disabled={busy}
              className="w-full rounded-lg bg-white px-3 py-2 font-medium text-slate-900 hover:bg-slate-100 disabled:opacity-50"
            >
              {busy ? "Opening Google…" : "Continue with Google"}
            </button>
          </>
        )}

        {needsInvite && (
          <form onSubmit={onRedeem} className="space-y-3">
            <p className="text-sm text-slate-300">
              Signed in as <span className="text-slate-100">{firebaseUser?.email}</span>.
              Enter your invite code to finish setup.
            </p>
            <input
              type="text"
              required
              autoFocus
              placeholder="invite code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              className="w-full rounded-lg bg-slate-800 px-3 py-2 outline-none ring-1 ring-slate-700 focus:ring-emerald-500"
            />
            <button
              type="submit"
              disabled={busy}
              className="w-full rounded-lg bg-emerald-500 px-3 py-2 font-medium text-slate-950 hover:bg-emerald-400 disabled:opacity-50"
            >
              {busy ? "Checking…" : "Redeem code"}
            </button>
            <button
              type="button"
              onClick={logout}
              className="w-full text-xs text-slate-400 hover:text-slate-200"
            >
              Cancel and sign out
            </button>
          </form>
        )}

        {error && <p className="text-sm text-rose-400">{error}</p>}
      </div>
    </div>
  );
}
