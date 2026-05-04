import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";

export default function TripsListPage() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [trips, setTrips] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api
      .get("/api/trips")
      .then(setTrips)
      .catch((e) => setError(e.message));
  }, []);

  async function onDelete(id) {
    if (!confirm("Delete this trip?")) return;
    try {
      await api.del(`/api/trips/${id}`);
      setTrips((cur) => (cur || []).filter((t) => t.id !== id));
    } catch (e) {
      alert(e.message);
    }
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="sticky top-0 z-10 border-b border-slate-800 bg-slate-950/90 backdrop-blur">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
          <div className="flex items-center gap-4">
            <Link to="/" className="text-lg font-semibold">
              Backcountry
            </Link>
            <span className="text-slate-500">/</span>
            <span className="text-slate-300">My Trips</span>
          </div>
          <div className="flex items-center gap-3 text-sm">
            <Link to="/" className="text-slate-300 hover:text-white">
              New trip
            </Link>
            <Link to="/profile" className="text-slate-300 hover:text-white">
              {user?.display_name || user?.email}
            </Link>
            <button
              onClick={async () => {
                await logout();
                navigate("/");
              }}
              className="rounded-lg border border-slate-700 px-3 py-1 hover:border-slate-500"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-6 py-8">
        {error && <p className="mb-4 text-rose-400">{error}</p>}
        {trips === null && <p className="text-slate-400">Loading…</p>}
        {trips && trips.length === 0 && (
          <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-8 text-center">
            <p className="text-slate-300">You haven't saved any trips yet.</p>
            <Link
              to="/"
              className="mt-3 inline-block rounded-lg bg-teal-500 px-4 py-2 font-medium text-slate-950 hover:bg-teal-400"
            >
              Plan your first trip
            </Link>
          </div>
        )}
        {trips && trips.length > 0 && (
          <ul className="grid gap-3 sm:grid-cols-2">
            {trips.map((t) => (
              <li
                key={t.id}
                className="rounded-xl border border-slate-800 bg-slate-900/50 p-4 hover:border-slate-600"
              >
                <Link to={`/trips/${t.id}`} className="block">
                  <h3 className="text-base font-medium text-white">{t.name}</h3>
                  <p className="mt-1 text-xs text-slate-400">
                    {t.start_date && t.end_date
                      ? `${t.start_date} → ${t.end_date}`
                      : "No dates set"}
                  </p>
                  {t.selected_trail?.area && (
                    <p className="mt-1 text-xs text-slate-500">{t.selected_trail.area}</p>
                  )}
                </Link>
                <div className="mt-3 flex items-center justify-between text-xs">
                  {t.share_token ? (
                    <span className="text-teal-400">Shared</span>
                  ) : (
                    <span className="text-slate-500">Private</span>
                  )}
                  <button
                    onClick={() => onDelete(t.id)}
                    className="text-rose-400 hover:text-rose-300"
                  >
                    Delete
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </main>
    </div>
  );
}
