import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

export default function AccountMenu() {
  const { user, loading, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();

  if (loading) return null;

  if (!user) {
    return (
      <div className="fixed top-3 right-3 z-50 flex gap-2">
        <Link
          to="/login"
          className="rounded-full bg-slate-900/80 px-3 py-1 text-xs font-medium text-white shadow-lg backdrop-blur hover:bg-slate-800"
        >
          Sign in
        </Link>
        <Link
          to="/register"
          className="rounded-full bg-emerald-500/90 px-3 py-1 text-xs font-semibold text-slate-950 shadow-lg hover:bg-emerald-400"
        >
          Sign up
        </Link>
      </div>
    );
  }

  const initial = (user.display_name || user.email || "?")[0].toUpperCase();

  return (
    <div className="fixed top-3 right-3 z-50">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex h-9 w-9 items-center justify-center rounded-full bg-emerald-500 text-sm font-bold text-slate-950 shadow-lg hover:bg-emerald-400"
        aria-label="Account menu"
      >
        {initial}
      </button>
      {open && (
        <div className="absolute right-0 mt-2 w-48 overflow-hidden rounded-xl border border-slate-700 bg-slate-900 text-sm shadow-2xl">
          <div className="border-b border-slate-800 px-4 py-2 text-xs text-slate-400">
            {user.email}
          </div>
          <Link
            to="/trips"
            onClick={() => setOpen(false)}
            className="block px-4 py-2 text-slate-200 hover:bg-slate-800"
          >
            My Trips
          </Link>
          <Link
            to="/profile"
            onClick={() => setOpen(false)}
            className="block px-4 py-2 text-slate-200 hover:bg-slate-800"
          >
            Profile
          </Link>
          <button
            onClick={async () => {
              setOpen(false);
              await logout();
              navigate("/");
            }}
            className="block w-full px-4 py-2 text-left text-rose-300 hover:bg-slate-800"
          >
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}
