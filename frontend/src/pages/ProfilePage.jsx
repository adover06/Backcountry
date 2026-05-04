import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

export default function ProfilePage() {
  const { user, updateProfile, logout } = useAuth();
  const navigate = useNavigate();
  const [displayName, setDisplayName] = useState(user?.display_name || "");
  const [units, setUnits] = useState(user?.preferences?.units || "imperial");
  const [defaultRegion, setDefaultRegion] = useState(
    user?.preferences?.default_region || ""
  );
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [error, setError] = useState(null);

  async function onSave(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      await updateProfile({
        display_name: displayName || null,
        preferences: { units, default_region: defaultRegion || null },
      });
      setMsg("Saved.");
    } catch (err) {
      setError(err.message || "Save failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 px-6 py-4">
        <div className="mx-auto flex max-w-3xl items-center justify-between">
          <Link to="/trips" className="text-teal-400 hover:underline">
            ← My Trips
          </Link>
          <button
            onClick={async () => {
              await logout();
              navigate("/");
            }}
            className="rounded-lg border border-slate-700 px-3 py-1 text-sm hover:border-slate-500"
          >
            Sign out
          </button>
        </div>
      </header>

      <form onSubmit={onSave} className="mx-auto max-w-3xl space-y-4 p-6">
        <h1 className="text-xl font-semibold">Profile</h1>
        <p className="text-sm text-slate-400">{user?.email}</p>

        <label className="block">
          <span className="text-xs uppercase tracking-wide text-slate-400">Display name</span>
          <input
            type="text"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            className="mt-1 w-full rounded-lg bg-slate-800 px-3 py-2 outline-none ring-1 ring-slate-700 focus:ring-teal-500"
          />
        </label>

        <label className="block">
          <span className="text-xs uppercase tracking-wide text-slate-400">Units</span>
          <select
            value={units}
            onChange={(e) => setUnits(e.target.value)}
            className="mt-1 w-full rounded-lg bg-slate-800 px-3 py-2 outline-none ring-1 ring-slate-700 focus:ring-teal-500"
          >
            <option value="imperial">Imperial (mi / ft)</option>
            <option value="metric">Metric (km / m)</option>
          </select>
        </label>

        <label className="block">
          <span className="text-xs uppercase tracking-wide text-slate-400">
            Default region
          </span>
          <input
            type="text"
            placeholder="e.g. Sierra Nevada"
            value={defaultRegion}
            onChange={(e) => setDefaultRegion(e.target.value)}
            className="mt-1 w-full rounded-lg bg-slate-800 px-3 py-2 outline-none ring-1 ring-slate-700 focus:ring-teal-500"
          />
        </label>

        {error && <p className="text-sm text-rose-400">{error}</p>}
        {msg && <p className="text-sm text-teal-400">{msg}</p>}

        <button
          type="submit"
          disabled={busy}
          className="rounded-lg bg-teal-500 px-4 py-2 font-medium text-slate-950 hover:bg-teal-400 disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save"}
        </button>
      </form>
    </div>
  );
}
