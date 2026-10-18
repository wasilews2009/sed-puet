/**
 * Вхід через Keycloak: OIDC Authorization Code + PKCE (S256), публічний клієнт без секрету.
 * redirect_uri — корінь застосунку (маршрутизація SPA на #hash, тож Keycloak не бачить внутрішніх шляхів).
 */
import type { AuthConfig } from "../api/types";
import { readSession, saveSession, clearSession } from "./session";

const PKCE_KEY = "sed.pkce";

export function b64url(buf: ArrayBuffer | Uint8Array): string {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString(): string {
  return b64url(crypto.getRandomValues(new Uint8Array(32)));
}

export async function pkceChallenge(verifier: string): Promise<string> {
  return b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
}

function redirectUri(): string {
  return `${location.origin}/`;
}

export async function startLogin(cfg: AuthConfig): Promise<void> {
  if (!cfg.issuer || !cfg.client_id) throw new Error("Сервер не повідомив адресу Keycloak");
  const verifier = randomString();
  const state = randomString();
  const nonce = randomString();
  sessionStorage.setItem(PKCE_KEY, JSON.stringify({ verifier, state, nonce, back: location.hash }));
  const url = new URL(`${cfg.issuer}/protocol/openid-connect/auth`);
  url.search = new URLSearchParams({
    client_id: cfg.client_id,
    redirect_uri: redirectUri(),
    response_type: "code",
    scope: "openid",
    code_challenge: await pkceChallenge(verifier),
    code_challenge_method: "S256",
    state,
    nonce,
  }).toString();
  location.assign(url.toString());
}

/** Повернення з Keycloak (?code=…&state=…): обмін коду на токен. Повертає true, якщо це був callback. */
export async function completeLogin(cfg: AuthConfig): Promise<boolean> {
  const params = new URLSearchParams(location.search);
  const code = params.get("code");
  const error = params.get("error");
  if (!code && !error) return false;
  const saved = JSON.parse(sessionStorage.getItem(PKCE_KEY) || "{}") as {
    verifier?: string;
    state?: string;
    back?: string;
  };
  sessionStorage.removeItem(PKCE_KEY);
  history.replaceState(null, "", "/" + (saved.back || ""));
  if (error) throw new Error(`Keycloak відмовив у вході: ${params.get("error_description") || error}`);
  if (!saved.verifier || params.get("state") !== saved.state) {
    throw new Error("Невірний стан входу (state). Спробуйте ще раз");
  }
  const r = await fetch(`${cfg.issuer}/protocol/openid-connect/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code: code!,
      redirect_uri: redirectUri(),
      client_id: String(cfg.client_id),
      code_verifier: saved.verifier,
    }),
  });
  if (!r.ok) throw new Error("Keycloak не видав токен");
  const body = (await r.json()) as { access_token: string; expires_in: number; id_token?: string };
  saveSession(body.access_token, body.expires_in, body.id_token);
  return true;
}

export function logoutUrl(cfg: AuthConfig): string | null {
  const s = readSession();
  clearSession();
  if (cfg.mode !== "keycloak" || !cfg.issuer || !cfg.client_id) return null;
  const url = new URL(`${cfg.issuer}/protocol/openid-connect/logout`);
  url.search = new URLSearchParams({
    client_id: cfg.client_id,
    post_logout_redirect_uri: redirectUri(),
    ...(s?.idToken ? { id_token_hint: s.idToken } : {}),
  }).toString();
  return url.toString();
}
