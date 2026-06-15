import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";

const METRIC_LABELS = [
  ["users", "Users"],
  ["trips", "Saved trips"],
  ["saved_trails", "Saved trails"],
  ["invites_available", "Invites available"],
  ["invites_redeemed", "Invites redeemed"],
  ["active_share_links", "Active share links"],
];

export default function AdminPage() {
  const [invites, setInvites] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [error, setError] = useState(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  async function refresh() {
    try {
      const [inv, met] = await Promise.all([
        api.get("/api/admin/invites"),
        api.get("/api/admin/metrics").catch(() => null),
      ]);
      setInvites(inv);
      setMetrics(met);
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function mint(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post("/api/admin/invites", { note: note || null });
      setNote("");
      await refresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function revoke(code) {
    if (!confirm(`Revoke code ${code}?`)) return;
    try {
      await api.del(`/api/admin/invites/${code}`);
      await refresh();
    } catch (e) {
      alert(e.message);
    }
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 px-6 py-4">
        <div className="mx-auto flex max-w-4xl items-center justify-between">
          <Link to="/" className="text-teal-400 hover:underline">
            ← Home
          </Link>
          <h1 className="text-base font-semibold">Admin · Invite codes</h1>
        </div>
      </header>

      <main className="mx-auto max-w-4xl space-y-6 p-6">
        {metrics && (
          <section>
            <h2 className="mb-3 text-xs uppercase tracking-wide text-slate-400">
              Overview
            </h2>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              {METRIC_LABELS.map(([key, label]) => (
                <div
                  key={key}
                  className="rounded-xl border border-slate-800 bg-slate-900 px-4 py-3"
                >
                  <div className="text-2xl font-semibold text-emerald-400">
                    {metrics[key] ?? 0}
                  </div>
                  <div className="text-xs text-slate-400">{label}</div>
                </div>
              ))}
            </div>
          </section>
        )}

        <form onSubmit={mint} className="flex gap-2">
          <input
            type="text"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Note (e.g. 'Jake from REI')"
            className="flex-1 rounded-lg bg-slate-800 px-3 py-2 outline-none ring-1 ring-slate-700 focus:ring-emerald-500"
          />
          <button
            type="submit"
            disabled={busy}
            className="rounded-lg bg-emerald-500 px-4 py-2 font-medium text-slate-950 hover:bg-emerald-400 disabled:opacity-50"
          >
            Mint code
          </button>
        </form>

        {error && <p className="text-sm text-rose-400">{error}</p>}

        {invites === null ? (
          <p className="text-slate-400">Loading…</p>
        ) : invites.length === 0 ? (
          <p className="text-slate-500">No codes yet.</p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="py-2">Code</th>
                <th>Note</th>
                <th>Created</th>
                <th>Status</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {invites.map((inv) => (
                <tr key={inv.code} className="border-t border-slate-800">
                  <td className="py-2 font-mono">
                    <button
                      onClick={() => navigator.clipboard.writeText(inv.code)}
                      className="hover:text-emerald-400"
                      title="Copy"
                    >
                      {inv.code}
                    </button>
                  </td>
                  <td className="text-slate-300">{inv.note || "—"}</td>
                  <td className="text-slate-500">
                    {new Date(inv.created_at).toLocaleDateString()}
                  </td>
                  <td>
                    {inv.redeemed_by ? (
                      <span className="text-slate-400">Redeemed</span>
                    ) : (
                      <span className="text-emerald-400">Available</span>
                    )}
                  </td>
                  <td className="text-right">
                    {!inv.redeemed_by && (
                      <button
                        onClick={() => revoke(inv.code)}
                        className="text-xs text-rose-400 hover:text-rose-300"
                      >
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </main>
    </div>
  );
}
