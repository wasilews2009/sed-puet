// @ts-check
/** Побудова DOM без innerHTML: усі дані з сервера потрапляють у сторінку як текст (захист від XSS). */

/** @typedef {Node | string | number | null | undefined | false | any[]} Child */

/**
 * @param {string} tag
 * @param {Record<string, any> | null} [attrs]
 * @param {...Child} children
 * @returns {HTMLElement}
 */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = String(v);
    else if (k === "style") {
      // Через CSSOM, а не атрибут: CSP `style-src 'self'` забороняє inline-атрибути style.
      for (const decl of String(v).split(";")) {
        const i = decl.indexOf(":");
        if (i > 0) el.style.setProperty(decl.slice(0, i).trim(), decl.slice(i + 1).trim());
      }
    }
    else if (k === "value" && "value" in el) /** @type {HTMLInputElement} */ (el).value = String(v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
}

/** @param {Node} el @param {Child[]} children */
function append(el, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else el.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
  }
}

/** @param {Element} el @param {...Child} children */
export function mount(el, ...children) {
  el.replaceChildren();
  append(el, children);
}

/** @param {string | null | undefined} iso */
export function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso.length === 10 ? iso + "T00:00:00" : iso);
  return d.toLocaleDateString("uk-UA");
}

/** @param {string | null | undefined} iso */
export function fmtDateTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("uk-UA", { dateStyle: "short", timeStyle: "short" });
}

/** @param {number} bytes */
export function fmtSize(bytes) {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
}

/**
 * Впевненість ШІ зі смужкою; нижче порогу — попередження (ручна валідація).
 * @param {string | number | null | undefined} value
 * @param {number} threshold
 */
export function confidence(value, threshold) {
  if (value === null || value === undefined) return h("span", { class: "muted" }, "—");
  const v = Number(value);
  const low = v < threshold;
  return h("span", { class: `conf${low ? " low" : ""}`, title: low ? `Нижче порогу ${threshold}: потрібна перевірка людиною` : "Впевненість моделі" },
    h("span", { class: "bar" }, h("i", { style: `width:${Math.round(v * 100)}%` })),
    `${(v * 100).toFixed(0)}%`);
}

/** @param {string} text @param {"" | "ok" | "warn" | "danger" | "accent"} [tone] */
export function pill(text, tone = "") {
  return h("span", { class: `pill ${tone}` }, text);
}

/** @param {string} text @param {"" | "ok" | "warn" | "danger"} [tone] */
export function notice(text, tone = "") {
  return h("div", { class: `notice ${tone}`, role: tone === "danger" ? "alert" : "status" }, text);
}

/** @param {string} message @param {"ok" | "danger" | ""} [tone] */
export function toast(message, tone = "ok") {
  const el = h("div", { class: `notice toast ${tone}`, role: "status" }, message);
  document.body.appendChild(el);
  setTimeout(() => el.remove(), tone === "danger" ? 9000 : 4000);
}

export function spinner() {
  return h("span", { class: "spinner", "aria-hidden": "true" });
}

/**
 * Кнопка, що блокується на час асинхронної дії і показує помилку.
 * @param {string} label
 * @param {() => Promise<unknown>} action
 * @param {Record<string, any>} [attrs]
 */
export function actionButton(label, action, attrs = {}) {
  /** @type {HTMLButtonElement} */
  const btn = /** @type {any} */ (h("button", { type: "button", ...attrs }, label));
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    const original = [...btn.childNodes];
    btn.replaceChildren(spinner(), " ", label);
    try {
      await action();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), "danger");
    } finally {
      btn.disabled = false;
      btn.replaceChildren(...original);
    }
  });
  return btn;
}

/**
 * Значення реквізиту: рядок, особа {full_name, position, …} або список осіб.
 * @param {any} value
 * @returns {Child}
 */
export function requisiteValue(value) {
  if (value === null || value === undefined || value === "") return h("span", { class: "muted" }, "не визначено");
  if (Array.isArray(value)) {
    if (!value.length) return h("span", { class: "muted" }, "немає");
    return h("ul", { style: "margin:0;padding-left:1.1rem" }, value.map((v) => h("li", null, requisiteValue(v))));
  }
  if (typeof value === "object") {
    const parts = [value.full_name, value.position, value.org_unit].filter(Boolean).join(", ");
    return parts || h("span", { class: "muted" }, "не визначено");
  }
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(value))) return fmtDate(String(value));
  return String(value);
}
