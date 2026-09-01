// Centralized API client. Every request gets a fresh Firebase ID token attached
// as a Bearer header when the user is signed in. Firebase auto-refreshes tokens,
// so no manual refresh loop is needed.

import { auth } from "../auth/firebase";

const API_BASE = import.meta.env.VITE_API_BASE || "";

async function authHeader(extraHeaders = {}) {
  const headers = { ...extraHeaders };
  const user = auth?.currentUser;
  if (user) {
    const token = await user.getIdToken();
    headers["Authorization"] = `Bearer ${token}`;
  }
  return headers;
}

// Drop-in replacement for window.fetch that attaches the Firebase bearer token
// and prefixes the API base. Returns the raw Response so existing call sites that
// inspect `res.ok` / `res.json()` keep working. Use for the planner/checks
// endpoints in App.jsx which now require authentication.
export async function authedFetch(path, init = {}) {
  const headers = await authHeader(init.headers || {});
  return fetch(`${API_BASE}${path}`, { ...init, headers });
}

async function request(path, { method = "GET", body, headers, isForm } = {}) {
  const baseHeaders = await authHeader(headers);
  const opts = { method, headers: baseHeaders };
  if (body !== undefined) {
    if (isForm) {
      opts.body = body;
    } else {
      opts.body = JSON.stringify(body);
      opts.headers["Content-Type"] = "application/json";
    }
  }
  return fetch(`${API_BASE}${path}`, opts);
}

async function parse(res) {
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function expectOk(res) {
  if (!res.ok) {
    const body = await parse(res);
    const detail = (body && body.detail) || res.statusText || "Request failed";
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return parse(res);
}

export const api = {
  base: API_BASE,
  get: (path) => request(path).then(expectOk),
  post: (path, body) => request(path, { method: "POST", body }).then(expectOk),
  patch: (path, body) => request(path, { method: "PATCH", body }).then(expectOk),
  del: (path) => request(path, { method: "DELETE" }).then(expectOk),
  postForm: (path, formData) =>
    request(path, { method: "POST", body: formData, isForm: true }).then(expectOk),
  rawGet: (path) => request(path),
};
