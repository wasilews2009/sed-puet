// @ts-check
/** Черга погодження: активні кроки, призначені користувачу (візи, погодження, підписи). */

import { api } from "../api.js";
import { fmtDateTime, h, mount, pill } from "../dom.js";
import { L } from "../state.js";

/** @param {HTMLElement} el */
export async function renderInbox(el) {
  /** @type {any[]} */
  const items = await api("/api/v1/approvals/inbox");
  mount(el, h("h1", null, "На погодження"),
    h("p", { class: "muted" }, "Документи, що чекають вашої візи або підпису. Наступний учасник маршруту отримає документ лише після вашого рішення."),
    items.length ? h("div", { class: "card table-wrap", style: "padding:0" }, h("table", null,
      h("thead", null, h("tr", null, ["Документ", "Підрозділ / автор", "Що потрібно", "Отримано", "Строк"].map((c) => h("th", null, c)))),
      h("tbody", null, items.map((x) => h("tr", { class: "clickable", onclick: () => { location.hash = `#/documents/${x.document_id}/route`; } },
        h("td", null, h("a", { href: `#/documents/${x.document_id}/route` }, x.document_title),
          h("div", { class: "small muted" }, x.document_type_name), x.delegated_from ? h("div", { class: "small" }, `замість ${x.delegated_from}`) : null),
        h("td", { class: "small" }, x.org_unit_name, h("div", { class: "muted" }, x.author_name)),
        h("td", null, h("div", { class: "row" }, pill(L("StepKind", x.step_kind), "accent"), x.requires_kep ? pill("КЕП", "") : null),
          x.acting_role ? h("div", { class: "small muted" }, `як ${L("UserRole", x.acting_role).toLowerCase()}`) : null),
        h("td", { class: "small nowrap" }, fmtDateTime(x.activated_at)),
        h("td", { class: "small nowrap" }, x.due_at ? fmtDateTime(x.due_at) : "—", x.is_overdue ? h("div", null, pill("прострочено", "danger")) : null))))))
      : h("div", { class: "card empty" }, "Немає документів, що чекають вашого рішення."));
}
