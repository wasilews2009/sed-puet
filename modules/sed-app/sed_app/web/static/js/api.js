// @ts-check
/** Клієнт API СЕД: Bearer-токен, X-Request-ID, помилки українською з поля `detail`. */

import { session } from "./auth.js";

export class ApiError extends Error {
  /** @param {number} status @param {string} message @param {string} [code] */
  constructor(status, message, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/** @param {any} body */
function detail(body) {
  if (!body) return null;
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) {
    // Помилки валідації FastAPI/Pydantic
    return body.detail.map((/** @type {any} */ d) => String(d.msg || "").replace(/^Value error, /, "")).join("; ");
  }
  return null;
}

/**
 * @param {string} path
 * @param {{ method?: string, json?: any, form?: FormData, blob?: boolean, signal?: AbortSignal }} [opts]
 * @returns {Promise<any>}
 */
export async function api(path, opts = {}) {
  const s = session();
  /** @type {Record<string, string>} */
  const headers = { "X-Request-ID": crypto.randomUUID().replace(/-/g, "") };
  if (s) headers.Authorization = `Bearer ${s.token}`;
  let body;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.json);
  } else if (opts.form) {
    body = opts.form;
  }
  const r = await fetch(path, { method: opts.method || (body ? "POST" : "GET"), headers, body, signal: opts.signal });
  if (r.status === 401) {
    sessionStorage.removeItem("sed.session");
    location.hash = "#/login";
    throw new ApiError(401, "Сесія завершилась. Увійдіть знову");
  }
  if (!r.ok) {
    let parsed = null;
    try { parsed = await r.json(); } catch { /* не JSON */ }
    throw new ApiError(r.status, detail(parsed) || `Помилка сервера (${r.status})`, parsed?.code);
  }
  if (opts.blob) return r.blob();
  if (r.status === 204) return null;
  return r.json();
}

/** @param {Record<string, string | number | boolean | null | undefined>} params */
export function query(params) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "" && v !== false) q.set(k, String(v));
  const s = q.toString();
  return s ? `?${s}` : "";
}
