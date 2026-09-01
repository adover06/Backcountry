import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import App from "./App.jsx";
import "./index.css";
import { AuthProvider } from "./auth/AuthContext.jsx";
import RequireAuth from "./auth/RequireAuth.jsx";
import AdminPage from "./pages/AdminPage.jsx";
import DiscoverPage from "./pages/DiscoverPage.jsx";
import LoginPage from "./pages/LoginPage.jsx";
import ProfilePage from "./pages/ProfilePage.jsx";
import SharePage from "./pages/SharePage.jsx";
import TripDetailPage from "./pages/TripDetailPage.jsx";
import TripsListPage from "./pages/TripsListPage.jsx";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          {/* Discovery is the front door and is browsable without an account:
              trail geometry is public reference data, and a login wall in front of
              a map buys nothing. Saving trips, the planner, and admin still
              require auth. */}
          <Route path="/" element={<DiscoverPage />} />
          <Route
            path="/plan"
            element={
              <RequireAuth>
                <App />
              </RequireAuth>
            }
          />
          <Route path="/login" element={<LoginPage />} />
          <Route
            path="/trips"
            element={
              <RequireAuth>
                <TripsListPage />
              </RequireAuth>
            }
          />
          <Route
            path="/trips/:id"
            element={
              <RequireAuth>
                <TripDetailPage />
              </RequireAuth>
            }
          />
          <Route
            path="/profile"
            element={
              <RequireAuth>
                <ProfilePage />
              </RequireAuth>
            }
          />
          <Route
            path="/admin"
            element={
              <RequireAuth requireAdmin>
                <AdminPage />
              </RequireAuth>
            }
          />
          <Route path="/share/:token" element={<SharePage />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  </React.StrictMode>
);
