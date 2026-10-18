// @ts-check
/**
 * Вхід у СЕД.
 *  - keycloak: OIDC Authorization Code + PKCE (S256), публічний клієнт без секрету;
 *  - dev: вибір демо-користувача, токен підписує сам застосунок (лише стенд розробника).
 * Токен живе в sessionStorage вкладки (зникає при закритті); refresh-токен не зберігається:
 * після завершення строку дії Keycloak повертає новий токен за активною SSO-сесією.
 */

const KEY = "sed.session";

/** @typedef {{ mode: "keycloak" | "dev", issuer?: string | null, client_id?: string | null, environment: string }} AuthConfig */
/** @typedef {{ token: string, expires: number, idToken?: string }} Session */

/** @type {AuthConfig | null} */
let config = null;

/** @returns {Promise<AuthConfig>} */
export async function loadConfig() {
  if (!config) {
    const r = await fetch("/api/v1/auth/config");
    config = await r.json();
  }
  return /** @type {AuthConfig} */ (config);
}

/** @returns {Session | null} */
export function session() {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    /** @type {Session} */
    const s = JSON.parse(raw);
    return s.expires > Date.now() + 15_000 ? s : null;
  } catch {
    return null;
  }
}

/** @param {string} token @param {number} expiresIn @param {string} [idToken] */
function save(token, expiresIn, idToken) {
  sessionStorage.setItem(KEY, JSON.stringify({ token, expires: Date.now() + expiresIn * 1000, idToken }));
}

/** @param {string} userId */
export async function devLogin(userId) {
  const r = await fetch("/api/v1/auth/dev-login", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user_id: userId }),
  });
  if (!r.ok) throw new Error((await r.json()).detail || "Не вдалося увійти");
  const body = await r.json();
  save(body.access_token, body.expires_in);
}

/** @param {ArrayBuffer} buf */
function b64url(buf) {
  return btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString() {
  return b64url(crypto.getRandomValues(new Uint8Array(32)).buffer);
}

function redirectUri() {
  return `${location.origin}/`;
}

export async function startKeycloakLogin() {
  const cfg = await loadConfig();
  const verifier = randomString();
  const state = randomString();
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  sessionStorage.setItem("sed.pkce", JSON.stringify({ verifier, state, back: location.hash }));
  const url = new URL(`${cfg.issuer}/protocol/openid-connect/auth`);
  url.search = new URLSearchParams({
    client_id: String(cfg.client_id), redirect_uri: redirectUri(), response_type: "code", scope: "openid",
    code_challenge: challenge, code_challenge_method: "S256", state,
  }).toString();
  location.assign(url.toString());
}

/** Повернення з Keycloak: обмін коду на токен. @returns {Promise<boolean>} чи був це callback */
export async function handleCallback() {
  const params = new URLSearchParams(location.search);
  const code = params.get("code");
  if (!code) return false;
  const saved = JSON.parse(sessionStorage.getItem("sed.pkce") || "{}");
  sessionStorage.removeItem("sed.pkce");
  history.replaceState(null, "", "/" + (saved.back || ""));
  if (!saved.verifier || params.get("state") !== saved.state) throw new Error("Невірний стан входу (state). Спробуйте ще раз");
  const cfg = await loadConfig();
  const r = await fetch(`${cfg.issuer}/protocol/openid-connect/token`, {
    method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code", code, redirect_uri: redirectUri(), client_id: String(cfg.client_id),
      code_verifier: saved.verifier,
    }),
  });
  if (!r.ok) throw new Error("Keycloak не видав токен");
  const body = await r.json();
  save(body.access_token, body.expires_in, body.id_token);
  return true;
}

export async function logout() {
  const s = session();
  sessionStorage.removeItem(KEY);
  const cfg = await loadConfig();
  if (cfg.mode === "keycloak" && cfg.issuer) {
    const url = new URL(`${cfg.issuer}/protocol/openid-connect/logout`);
    url.search = new URLSearchParams({
      client_id: String(cfg.client_id), post_logout_redirect_uri: redirectUri(),
      ...(s?.idToken ? { id_token_hint: s.idToken } : {}),
    }).toString();
    location.assign(url.toString());
  } else {
    location.hash = "#/login";
    location.reload();
  }
}
