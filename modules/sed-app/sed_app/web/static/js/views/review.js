// @ts-check
/** Черга ручної валідації: результати ШІ з впевненістю нижче порогу (Human-in-the-loop). */

import { api } from "../api.js";
import { confidence, fmtDateTime, h, mount, pill } from "../dom.js";
import { threshold } from "../state.js";

/** @param {HTMLElement} el */
export async function renderReviewQueue(el) {
  /** @type {any[]} */
  const items = await api("/api/v1/review-queue");
  mount(el, h("h1", null, "Перевірка результатів ШІ"),
    h("p", { class: "muted" }, `Реквізити і звіти нормативного аудиту з впевненістю нижче ${threshold() * 100}% не потрапляють у картку, доки людина їх не підтвердить або не виправить.`),
    items.length ? h("div", { class: "card table-wrap", style: "padding:0" }, h("table", null,
      h("thead", null, h("tr", null, ["Документ", "Підрозділ", "Що перевірити", "Впевненість", "Надійшло"].map((c) => h("th", null, c)))),
      h("tbody", null, items.map((x) => {
        const tab = x.task === "requisites" ? "requisites" : "compliance";
        return h("tr", { class: "clickable", onclick: () => { location.hash = `#/documents/${x.document_id}/${tab}`; } },
          h("td", null, h("a", { href: `#/documents/${x.document_id}/${tab}` }, x.document_title)),
          h("td", { class: "small" }, x.org_unit_name),
          h("td", null, x.task === "requisites"
            ? [pill("Реквізити", "warn"), x.pending_fields ? h("span", { class: "small muted" }, ` ${x.pending_fields} знач.`) : h("span", { class: "small muted" }, " ввести вручну")]
            : pill("Звіт нормативного аудиту", "warn")),
          h("td", null, confidence(x.confidence, threshold())),
          h("td", { class: "small nowrap" }, fmtDateTime(x.queued_at)));
      }))))
      : h("div", { class: "card empty" }, "Черга порожня: усі результати ШІ у ваших документах перевірено."));
}
