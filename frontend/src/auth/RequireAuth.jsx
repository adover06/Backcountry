import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "./AuthContext";

export default function RequireAuth({ children, requireAdmin = false }) {
  const { appUser, needsInvite, loading } = useAuth();
  const location = useLocation();
  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center text-slate-400">
        Loading…
      </div>
    );
  }
  if (needsInvite) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }
  if (!appUser) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }
  if (requireAdmin && !appUser.is_admin) {
    return <Navigate to="/" replace />;
  }
  return children;
}
