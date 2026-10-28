// Fetch wrapper. Every write carries the X-Barometer header the server requires.

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export const bus = new EventTarget();

export async function api(path, { method = "GET", body, form } = {}) {
  const opts = { method, credentials: "same-origin", headers: {}, cache: "no-store" };
  if (method !== "GET") opts.headers["X-Barometer"] = "1";
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (form) opts.body = form;
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new ApiError(0, "Can't reach the Pi. Check Tailscale is connected.");
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (res.status === 401 && path !== "/api/login" && path !== "/api/deep/unlock") {
    bus.dispatchEvent(new Event("signedout"));
  }
  if (res.status === 423) bus.dispatchEvent(new Event("locked"));
  if (!res.ok) {
    const detail = data && data.detail;
    const message = typeof detail === "string" ? detail : Array.isArray(detail) ? "Check the form and try again" : res.statusText;
    throw new ApiError(res.status, message);
  }
  bus.dispatchEvent(new Event("activity"));
  return data;
}

export const get = (path) => api(path);
export const post = (path, body) => api(path, { method: "POST", body: body ?? {} });
export const patch = (path, body) => api(path, { method: "PATCH", body });
export const put = (path, body) => api(path, { method: "PUT", body });
export const del = (path) => api(path, { method: "DELETE" });
