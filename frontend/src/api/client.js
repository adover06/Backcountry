// Centralized API client. All requests include cookies; on 401 we attempt a single
// refresh round-trip before propagating the error.

const API_BASE = import.meta.env.VITE_API_BASE || "";

let refreshing = null;

async function attemptRefresh() {
  if (!refreshing) {
    refreshing = fetch(`${API_BASE}/api/auth/refresh`, {
      method: "POST",
      credentials: "include",
    })
      .then((r) => r.ok)
      .catch(() => false)
      .finally(() => {
        // allow another attempt on the next failure
        setTimeout(() => (refreshing = null), 0);
      });
  }
  return refreshing;
}

async function request(path, { method = "GET", body, headers, isForm } = {}) {
  const opts = {
    method,
    credentials: "include",
    headers: { ...(headers || {}) },
  };
  if (body !== undefined) {
    if (isForm) {
      opts.body = body; // FormData
    } else {
      opts.body = JSON.stringify(body);
      opts.headers["Content-Type"] = "application/json";
    }
  }

  let res = await fetch(`${API_BASE}${path}`, opts);
  if (res.status === 401 && path !== "/api/auth/refresh" && path !== "/api/auth/login") {
    const ok = await attemptRefresh();
    if (ok) {
      res = await fetch(`${API_BASE}${path}`, opts);
    }
  }
  return res;
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
