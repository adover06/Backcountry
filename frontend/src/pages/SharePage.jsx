import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import { TripView } from "./TripDetailPage";

export default function SharePage() {
  const { token } = useParams();
  const [trip, setTrip] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api
      .get(`/api/trips/share/${token}`)
      .then(setTrip)
      .catch((e) => setError(e.message));
  }, [token]);

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-950 text-rose-300 p-6">
        Share link not found or revoked.
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
      <header className="border-b border-slate-800 px-6 py-4">
        <span className="text-sm text-slate-400">
          Shared trip report · OpenTrails
        </span>
      </header>
      <TripView trip={trip} readOnly />
    </div>
  );
}
