/**
 * Сесія вкладки: access-токен у sessionStorage (зникає з закриттям вкладки).
 * Refresh-токен не зберігається свідомо: після спливання строку Keycloak видає новий токен
 * за активною SSO-сесією (повторний редирект без пароля), що зменшує наслідки XSS.
 */
const KEY = "sed.session";
const SKEW_MS = 15_000;

export interface Session {
  token: string;
  expires: number;
  idToken?: string;
}

export function readSession(now = Date.now()): Session | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as Session;
    return typeof s.token === "string" && s.expires > now + SKEW_MS ? s : null;
  } catch {
    return null;
  }
}

export function saveSession(token: string, expiresInSeconds: number, idToken?: string): void {
  const s: Session = { token, expires: Date.now() + expiresInSeconds * 1000, idToken };
  sessionStorage.setItem(KEY, JSON.stringify(s));
}

export function clearSession(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    /* сховище недоступне */
  }
}

export function currentToken(): string | null {
  return readSession()?.token ?? null;
}
