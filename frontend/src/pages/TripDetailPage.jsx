import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";

export default function TripDetailPage() {
  const { id } = useParams();
  const [trip, setTrip] = useState(null);
  const [error, setError] = useState(null);
  const [shareUrl, setShareUrl] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .get(`/api/trips/${id}`)
      .then((t) => {
        setTrip(t);
        if (t.share_token) {
          setShareUrl(`${window.location.origin}/share/${t.share_token}`);
        }
      })
      .catch((e) => setError(e.message));
  }, [id]);

  async function onShare() {
    setBusy(true);
    try {
      const out = await api.post(`/api/trips/${id}/share`);
      setShareUrl(`${window.location.origin}/share/${out.token}`);
      setTrip((t) => ({ ...t, share_token: out.token }));
    } catch (e) {
      alert(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function onRevoke() {
    setBusy(true);
    try {
      await api.del(`/api/trips/${id}/share`);
      setShareUrl(null);
      setTrip((t) => ({ ...t, share_token: null }));
    } catch (e) {
      alert(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (error) {
    return (
      <div className="min-h-screen bg-slate-950 text-rose-300 p-6">
        <Link to="/trips" className="text-teal-400 hover:underline">
          ← Back
        </Link>
        <p className="mt-4">{error}</p>
      </div>
    );
  }
  if (!trip) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-950 text-slate-400">
        Loading…
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="sticky top-0 z-10 border-b border-slate-800 bg-slate-950/90 backdrop-blur">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
          <Link to="/trips" className="text-teal-400 hover:underline">
            ← My Trips
          </Link>
          <div className="flex items-center gap-2">
            {shareUrl ? (
              <>
                <button
                  onClick={() => navigator.clipboard.writeText(shareUrl)}
                  className="rounded-lg border border-slate-700 px-3 py-1 text-sm hover:border-slate-500"
                >
                  Copy share URL
                </button>
                <button
                  onClick={onRevoke}
                  disabled={busy}
                  className="rounded-lg border border-rose-700 px-3 py-1 text-sm text-rose-300 hover:border-rose-500"
                >
                  Revoke
                </button>
              </>
            ) : (
              <button
                onClick={onShare}
                disabled={busy}
                className="rounded-lg bg-teal-500 px-3 py-1 text-sm font-medium text-slate-950 hover:bg-teal-400 disabled:opacity-50"
              >
                Create share link
              </button>
            )}
          </div>
        </div>
      </header>
      <TripView trip={trip} shareUrl={shareUrl} />
    </div>
  );
}

export function TripView({ trip, shareUrl, readOnly = false }) {
  return (
    <main className="mx-auto max-w-5xl px-6 py-8 space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">{trip.name}</h1>
        <p className="mt-1 text-sm text-slate-400">
          {trip.start_date && trip.end_date
            ? `${trip.start_date} → ${trip.end_date}`
            : "No dates set"}
          {trip.selected_trail?.area && ` · ${trip.selected_trail.area}`}
        </p>
        {shareUrl && !readOnly && (
          <p className="mt-2 break-all text-xs text-teal-400">{shareUrl}</p>
        )}
      </div>

      {trip.report?.bullets?.length > 0 && (
        <section>
          <h2 className="mb-2 text-sm uppercase tracking-wide text-slate-400">Briefing</h2>
          <ul className="space-y-1 rounded-xl border border-slate-800 bg-slate-900/50 p-4">
            {trip.report.bullets.map((b, i) => (
              <li key={i} className="text-sm text-slate-200">
                • {typeof b === "string" ? b : b.text || JSON.stringify(b)}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="grid gap-4 sm:grid-cols-2">
        <Card title="Route">
          {trip.route ? (
            <ul className="text-sm text-slate-300">
              <li>Length: {trip.route.length_miles?.toFixed?.(1) ?? "?"} mi</li>
              <li>Elev gain: {trip.route.elev_gain_ft ?? "?"} ft</li>
            </ul>
          ) : (
            <p className="text-sm text-slate-500">No route data.</p>
          )}
        </Card>
        <Card title="Risk">
          {trip.checks ? (
            <ul className="text-sm text-slate-300">
              <li>AQI: {trip.checks.aqi?.observations?.[0]?.AQI ?? "—"}</li>
              <li>Fire perimeters: {trip.checks.fire?.count ?? 0}</li>
              <li>
                Snow max depth:{" "}
                {trip.checks.snow?.max_depth_in?.toFixed?.(1) ?? "—"} in
              </li>
            </ul>
          ) : (
            <p className="text-sm text-slate-500">No checks recorded.</p>
          )}
        </Card>
      </section>
    </main>
  );
}

function Card({ title, children }) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-4">
      <h3 className="mb-2 text-xs uppercase tracking-wide text-slate-400">{title}</h3>
      {children}
    </div>
  );
}
