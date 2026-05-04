import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";

export default function SaveTripButton({ planResult, selectedTrail, startDate, endDate }) {
  const { user } = useAuth();
  const [savedId, setSavedId] = useState(null);
  const [shareUrl, setShareUrl] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  if (!planResult) return null;

  async function onSave() {
    if (!user) return;
    setBusy(true);
    setError(null);
    try {
      const name =
        selectedTrail?.name ||
        planResult?.selected_trail?.name ||
        "Untitled trip";
      const trip = await api.post("/api/trips", {
        name,
        start_date: startDate || null,
        end_date: endDate || null,
        route: planResult.route,
        selected_trail: planResult.selected_trail || selectedTrail,
        checks: planResult.checks,
        report: planResult.report,
      });
      setSavedId(trip.id);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function onShare() {
    if (!savedId) return;
    setBusy(true);
    try {
      const out = await api.post(`/api/trips/${savedId}/share`);
      setShareUrl(`${window.location.origin}/share/${out.token}`);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (!user) {
    return (
      <div className="fixed bottom-4 left-4 z-40 rounded-xl border border-slate-700 bg-slate-900/90 px-4 py-3 text-sm text-slate-200 shadow-2xl backdrop-blur">
        <Link to="/login" className="font-medium text-emerald-400 hover:underline">
          Sign in
        </Link>{" "}
        to save this trip.
      </div>
    );
  }

  if (savedId) {
    return (
      <div className="fixed bottom-4 left-4 z-40 flex flex-col gap-2 rounded-xl border border-slate-700 bg-slate-900/90 p-3 text-sm text-slate-200 shadow-2xl backdrop-blur">
        <div className="flex items-center gap-2">
          <span className="text-emerald-400">✓ Saved.</span>
          <Link
            to={`/trips/${savedId}`}
            className="text-emerald-400 hover:underline"
          >
            View
          </Link>
          {!shareUrl && (
            <button
              onClick={onShare}
              disabled={busy}
              className="rounded-lg bg-emerald-500 px-2 py-1 text-xs font-medium text-slate-950 hover:bg-emerald-400"
            >
              Create share link
            </button>
          )}
        </div>
        {shareUrl && (
          <div className="flex items-center gap-2">
            <span className="break-all text-xs text-slate-400">{shareUrl}</span>
            <button
              onClick={() => navigator.clipboard.writeText(shareUrl)}
              className="rounded border border-slate-600 px-2 py-1 text-xs hover:border-slate-400"
            >
              Copy
            </button>
          </div>
        )}
        {error && <p className="text-xs text-rose-400">{error}</p>}
      </div>
    );
  }

  return (
    <div className="fixed bottom-4 left-4 z-40 flex items-center gap-2 rounded-xl border border-slate-700 bg-slate-900/90 px-3 py-2 text-sm text-slate-200 shadow-2xl backdrop-blur">
      <button
        onClick={onSave}
        disabled={busy}
        className="rounded-lg bg-emerald-500 px-3 py-1 text-sm font-semibold text-slate-950 hover:bg-emerald-400 disabled:opacity-50"
      >
        {busy ? "Saving…" : "Save trip"}
      </button>
      {error && <span className="text-xs text-rose-400">{error}</span>}
    </div>
  );
}
