// Thin fetch wrapper around the Django REST API. Token auth (DRF TokenAuthentication)
// with the token persisted in localStorage; brand-scoped lists take ?organization=&brand=.

const TOKEN_KEY = "deodap_care_token";

// The API lives under the same base path the app is served from. import.meta.env.BASE_URL
// is "/" in dev and the Vite `base` (e.g. "/email_automation/") in a sub-path build, so
// API calls resolve to "/api/..." locally and "/email_automation/api/..." in production.
export const API = `${import.meta.env.BASE_URL}api`.replace(/\/{2,}/g, "/");

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}
export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

function buildQuery(params) {
  const q = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") q.append(k, v);
  });
  const s = q.toString();
  return s ? `?${s}` : "";
}

export class ApiError extends Error {
  constructor(status, data) {
    super(data?.detail || `Request failed (${status})`);
    this.status = status;
    this.data = data;
  }
}

async function request(method, path, { params, body } = {}) {
  const headers = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Token ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const res = await fetch(`${API}${path}${buildQuery(params)}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (res.status === 204) return null;
  let data = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

async function requestForm(path, formData) {
  const headers = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Token ${token}`;   // NO Content-Type: browser sets it
  const res = await fetch(`${API}${path}`, { method: "POST", headers, body: formData });
  let data = null;
  const text = await res.text();
  if (text) { try { data = JSON.parse(text); } catch { data = text; } }
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

export const api = {
  get: (path, params) => request("GET", path, { params }),
  post: (path, body, params) => request("POST", path, { body, params }),
  postForm: (path, formData) => requestForm(path, formData),
  put: (path, body, params) => request("PUT", path, { body, params }),
  patch: (path, body, params) => request("PATCH", path, { body, params }),
  del: (path, params) => request("DELETE", path, { params }),
};

// --- auth ---
export async function login(username, password) {
  const res = await fetch(`${API}/auth/login/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, data);
  setToken(data.token);
  return data.user || null;   // role + permissions for the session
}

export const me = () => api.get("/auth/me/");
export const logoutApi = () => api.post("/auth/logout/").catch(() => {});

// Auto-login (no credentials) -> opens the panel without a sign-in screen.
export async function guestLogin() {
  const res = await fetch(`${API}/auth/guest/`, { method: "POST" });
  if (!res.ok) throw new ApiError(res.status, await res.json().catch(() => ({})));
  const data = await res.json();
  setToken(data.token);
  return data.user || null;
}
