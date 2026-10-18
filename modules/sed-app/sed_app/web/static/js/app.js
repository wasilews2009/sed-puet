// @ts-check
/** Точка входу веб-інтерфейсу СЕД ПУЕТ: вхід, каркас сторінки, маршрутизація за #hash. */

import { api } from "./api.js";
import { devLogin, handleCallback, loadConfig, logout, session, startKeycloakLogin } from "./auth.js";
import { h, mount, notice, spinner } from "./dom.js";
import { state } from "./state.js";
import { renderCard } from "./views/card.js";
import { renderDocuments } from "./views/documents.js";
import { renderInbox } from "./views/inbox.js";
import { renderReviewQueue } from "./views/review.js";
import { renderUpload } from "./views/upload.js";

const root = /** @type {HTMLElement} */ (document.getElementById("root"));

/** @type {{ pattern: RegExp, title: string, nav?: string, render: (el: HTMLElement, ...args: string[]) => Promise<void> }[]} */
const ROUTES = [
  { pattern: /^#\/documents\/([0-9a-f-]{36})(?:\/(\w+))?$/, title: "Картка документа", render: renderCard },
  { pattern: /^#\/upload$/, title: "Реєстрація документа", nav: "#/upload", render: renderUpload },
  { pattern: /^#\/inbox$/, title: "На погодження", nav: "#/inbox", render: renderInbox },
  { pattern: /^#\/review$/, title: "Перевірка ШІ", nav: "#/review", render: renderReviewQueue },
  { pattern: /^#\/documents$/, title: "Документи", nav: "#/documents", render: renderDocuments },
];

/** @type {{ inbox: HTMLElement, review: HTMLElement } | null} */
let badges = null;

async function boot() {
  try {
    await handleCallback();
  } catch (e) {
    mount(root, h("main", null, notice(String(e instanceof Error ? e.message : e), "danger")));
    return;
  }
  if (!session()) return renderLogin();
  try {
    [state.me, state.dict] = await Promise.all([api("/api/v1/me"), api("/api/v1/dictionaries")]);
  } catch (e) {
    mount(root, h("main", null, notice(e instanceof Error ? e.message : String(e), "danger"),
      h("p", null, h("button", { type: "button", onclick: () => logout() }, "Вийти"))));
    return;
  }
  renderShell();
  window.addEventListener("hashchange", route);
  window.addEventListener("sed:counts", () => refreshCounts());   // після рішень у картці
  await route();
}

function renderShell() {
  const me = /** @type {import("./state.js").Me} */ (state.me);
  const inboxCount = h("span", { class: "count", hidden: true });
  const reviewCount = h("span", { class: "count", hidden: true });
  badges = { inbox: inboxCount, review: reviewCount };
  const roles = me.roles.map((r) => r.role_label + (r.org_unit_name ? ` (${r.org_unit_name})` : "")).join(", ");
  mount(root,
    h("header", { class: "topbar" },
      h("a", { class: "brand", href: "#/documents" }, h("span", { class: "logo" }, "СЕД"),
        h("span", null, "СЕД ПУЕТ", h("small", null, "документообіг з ШІ-асистентом"))),
      h("nav", { class: "main", "aria-label": "Розділи" },
        h("a", { href: "#/documents", "data-nav": "#/documents" }, "Документи"),
        state.dict?.modules.ingest ? h("a", { href: "#/upload", "data-nav": "#/upload" }, "Зареєструвати") : null,
        state.dict?.modules.routing ? h("a", { href: "#/inbox", "data-nav": "#/inbox" }, "На погодження", inboxCount) : null,
        h("a", { href: "#/review", "data-nav": "#/review" }, "Перевірка ШІ", reviewCount)),
      h("div", { class: "user" }, h("b", null, me.display_name),
        h("span", { class: "muted roles", title: roles }, roles || "ролей не призначено"),
        h("button", { type: "button", class: "link small", onclick: () => logout() }, "Вийти"))),
    h("main", { id: "main", tabindex: "-1" }),
    h("footer", null,
      "ШІ є асистентом: реквізити і зауваження потребують перевірки людиною (поріг впевненості ",
      `${Number(state.dict?.confidence_threshold) * 100}%`,
      "). Юридичної сили документ набуває лише після накладання КЕП посадовою особою."),
  );
  if (!me.roles.length) {
    const main = /** @type {HTMLElement} */ (root.querySelector("main"));
    main.before(h("div", { class: "card notice warn", style: "max-width:1280px;margin:1rem auto 0" },
      "Вам ще не призначено жодної ролі в СЕД. Зверніться до адміністратора, щоб отримати доступ до документів підрозділу."));
  }
  refreshCounts();
}

async function refreshCounts() {
  if (!badges) return;
  const set = (/** @type {HTMLElement} */ el, /** @type {number} */ n) => { el.textContent = String(n); el.hidden = n === 0; };
  try {
    const [inbox, review] = await Promise.all([
      state.dict?.modules.routing ? api("/api/v1/approvals/inbox") : Promise.resolve([]),
      api("/api/v1/review-queue"),
    ]);
    set(badges.inbox, inbox.length);
    set(badges.review, review.length);
  } catch { /* лічильники не критичні */ }
}

async function route() {
  const hash = location.hash || "#/documents";
  if (hash === "#/login") { location.hash = "#/documents"; return; }
  const main = /** @type {HTMLElement} */ (root.querySelector("main"));
  const r = ROUTES.find((x) => x.pattern.test(hash)) || ROUTES[ROUTES.length - 1];
  const args = (hash.match(r.pattern) || []).slice(1).filter(Boolean);
  for (const a of root.querySelectorAll("nav.main a")) a.classList.toggle("active", a.getAttribute("data-nav") === r.nav);
  document.title = `${r.title} — СЕД ПУЕТ`;
  mount(main, h("div", { class: "empty" }, spinner(), " Завантаження…"));
  try {
    await r.render(main, ...args);
  } catch (e) {
    mount(main, notice(e instanceof Error ? e.message : String(e), "danger"));
  }
  main.focus({ preventScroll: true });
}

async function renderLogin() {
  const cfg = await loadConfig();
  const body = h("div", { class: "card stack" });
  mount(root, h("main", { class: "login", id: "main" },
    h("h1", null, "СЕД ПУЕТ"),
    h("p", { class: "muted" }, "Система електронного документообігу Полтавського університету економіки і торгівлі з ШІ-асистентом."),
    body));
  if (cfg.mode === "keycloak") {
    mount(body, h("p", null, "Вхід через єдиний обліковий запис університету (Keycloak SSO)."),
      h("button", { type: "button", class: "primary", onclick: () => startKeycloakLogin() }, "Увійти через SSO ПУЕТ"));
    return;
  }
  mount(body, notice("Стенд розробника: вхід демо-користувачем без пароля. У production цей режим вимкнено.", "warn"),
    h("div", { class: "empty" }, spinner()));
  /** @type {{ id: string, display_name: string, email: string, roles: string[] }[]} */
  const users = await (await fetch("/api/v1/auth/dev-users")).json();
  mount(body, notice("Стенд розробника: вхід демо-користувачем без пароля. У production цей режим вимкнено.", "warn"),
    users.length ? h("div", { class: "users" }, users.map((u) =>
      h("button", { type: "button", onclick: async () => { await devLogin(u.id); location.hash = "#/documents"; location.reload(); } },
        h("b", null, u.display_name), h("small", null, u.roles.join(", ")), h("small", null, u.email))))
      : notice("У базі немає користувачів з ролями. Виконайте: python -m sed_app.seeds.demo load", "danger"));
}

boot();
