/**
 * Типізований клієнт API sed-app на openapi-fetch.
 * Кожен запит: Bearer-токен сесії, X-Request-ID (наскрізний ідентифікатор для журналу аудиту),
 * помилки — ApiError з повідомленням українською з поля `detail` відповіді.
 */
import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";
import { clearSession, currentToken } from "../auth/session";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly code?: string,
    readonly extra?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Повідомлення з тіла помилки FastAPI: рядок `detail` або список помилок валідації Pydantic. */
export function errorMessage(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      return d
        .map((x) => String((x as { msg?: unknown }).msg ?? "").replace(/^Value error, /, ""))
        .filter(Boolean)
        .join("; ");
    }
  }
  if (status === 503) return "Сервіс тимчасово недоступний. Спробуйте пізніше";
  if (status === 413) return "Файл завеликий";
  return `Помилка сервера (${status})`;
}

export function requestId(): string {
  return crypto.randomUUID().replace(/-/g, "");
}

type Listener = () => void;
const unauthorized = new Set<Listener>();
/** Підписка на 401: застосунок показує вхід (Keycloak SSO поверне нового токена без пароля). */
export function onUnauthorized(fn: Listener): () => void {
  unauthorized.add(fn);
  return () => unauthorized.delete(fn);
}

const auth: Middleware = {
  onRequest({ request }) {
    const token = currentToken();
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
    request.headers.set("X-Request-ID", requestId());
    return request;
  },
  async onResponse({ response }) {
    if (response.status === 401) {
      clearSession();
      unauthorized.forEach((fn) => fn());
    }
    if (!response.ok) {
      let body: unknown = null;
      try {
        body = await response.clone().json();
      } catch {
        /* не JSON */
      }
      const b = (body ?? {}) as Record<string, unknown>;
      const { detail: _d, code, ...extra } = b;
      throw new ApiError(response.status, errorMessage(body, response.status), typeof code === "string" ? code : undefined, extra);
    }
    return response;
  },
};

export const http = createClient<paths>({ baseUrl: "" });
http.use(auth);

/**
 * Розгортає результат openapi-fetch: помилки вже кинуто в middleware, тут лише дані.
 * Повертає `T` навіть для 204 (тоді — undefined).
 */
export async function unwrap<T>(p: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  const r = await p;
  return r.data as T;
}

/** Двійкові відповіді (оригінал файлу, PKCS#12, ланцюжок ЦСК): той самий auth, без JSON. */
export async function fetchBlob(path: string, init: RequestInit = {}): Promise<{ blob: Blob; fileName: string | null }> {
  const headers = new Headers(init.headers);
  const token = currentToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  headers.set("X-Request-ID", requestId());
  const r = await fetch(path, { ...init, headers });
  if (r.status === 401) {
    clearSession();
    unauthorized.forEach((fn) => fn());
  }
  if (!r.ok) {
    let body: unknown = null;
    try {
      body = await r.json();
    } catch {
      /* не JSON */
    }
    throw new ApiError(r.status, errorMessage(body, r.status));
  }
  return { blob: await r.blob(), fileName: dispositionName(r.headers.get("Content-Disposition")) };
}

/** Ім'я файлу з Content-Disposition (RFC 6266, з підтримкою filename*=UTF-8''…). */
export function dispositionName(header: string | null): string | null {
  if (!header) return null;
  const star = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header);
  if (star?.[1]) {
    try {
      return decodeURIComponent(star[1].trim());
    } catch {
      /* падаємо на звичайне ім'я */
    }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(header);
  return plain?.[1]?.trim() ?? null;
}
