import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

export default function LoginPage() {
  const { firebaseUser, appUser, needsInvite, firebaseEnabled, signInGoogle, redeemInvite, logout } =
    useAuth();
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
        <h1 className="text-xl font-semibold">OpenTrails</h1>
        <p className="-mt-2 text-xs text-slate-500">
          Trail discovery is public — an account is only needed to save trips and run
          condition checks.
        </p>

        {!firebaseEnabled && (
          <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-200">
            <p className="font-medium">Sign-in is not configured</p>
            <p className="mt-1 text-amber-200/80">
              Firebase web config is missing. Set VITE_FIREBASE_* in{" "}
              <code className="font-mono text-xs">frontend/.env</code> and rebuild. Browsing
              trails does not require an account.
            </p>
          </div>
        )}

        {/* Signed in with Google but the API rejected the token — almost always a
            missing Firebase service account on the backend. Without this branch the
            card renders empty and there is no way out. */}
        {firebaseEnabled && firebaseUser && !appUser && !needsInvite && (
          <div className="space-y-3">
            <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-200">
              <p className="font-medium">Signed in, but the server could not verify it</p>
              <p className="mt-1 text-amber-200/80">
                You are authenticated with Google as{" "}
                <span className="font-medium">{firebaseUser.email}</span>, but the API returned
                401. The backend needs a Firebase service account at{" "}
                <code className="font-mono text-xs">data/firebase-service-account.json</code>.
              </p>
            </div>
            <button
              onClick={() => logout()}
              className="w-full rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:border-slate-500"
            >
              Sign out
            </button>
            <a href="/" className="block text-center text-sm text-slate-400 underline">
              Browse trails without an account
            </a>
          </div>
        )}

        {firebaseEnabled && !firebaseUser && !needsInvite && (
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
            </p>
            <p className="text-xs text-slate-400">
              Enter an invite code to finish setup — or leave it blank and continue if this
              address is listed in <code className="font-mono">ADMIN_EMAILS</code>. Codes are
              rows in the database, not emailed.
            </p>
            {/* Not `required`: an admin has no code, and the server decides whether
                one is needed. Requiring it here made the admin path unreachable. */}
            <input
              type="text"
              autoFocus
              placeholder="invite code (optional for admins)"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              className="w-full rounded-lg bg-slate-800 px-3 py-2 outline-none ring-1 ring-slate-700 focus:ring-emerald-500"
            />
            <button
              type="submit"
              disabled={busy}
              className="w-full rounded-lg bg-emerald-500 px-3 py-2 font-medium text-slate-950 hover:bg-emerald-400 disabled:opacity-50"
            >
              {busy ? "Checking…" : code.trim() ? "Redeem code" : "Continue"}
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
