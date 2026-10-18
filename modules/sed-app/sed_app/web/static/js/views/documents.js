// @ts-check
/** Реєстр документів: пошук, фільтри, сторінки. Сервер повертає лише доступні користувачу документи. */

import { api, query } from "../api.js";
import { confidence, fmtDate, fmtDateTime, h, mount, pill } from "../dom.js";
import { state, status, threshold } from "../state.js";

const PAGE = 25;

/** @param {HTMLElement} el */
export async function renderDocuments(el) {
  const dict = /** @type {import("../state.js").Dictionaries} */ (state.dict);
  const saved = JSON.parse(sessionStorage.getItem("sed.filters") || "{}");
  const f = { q: "", status: "", type_code: "", mine: false, needs_review: false, offset: 0, ...saved };

  const q = /** @type {HTMLInputElement} */ (h("input", { type: "search", placeholder: "Заголовок, індекс, зміст…", value: f.q, "aria-label": "Пошук" }));
  const st = /** @type {HTMLSelectElement} */ (h("select", { "aria-label": "Статус" },
    h("option", { value: "" }, "Усі статуси"),
    Object.entries(dict.labels.DocumentStatus).map(([v, label]) => h("option", { value: v, selected: f.status === v }, label))));
  const tp = /** @type {HTMLSelectElement} */ (h("select", { "aria-label": "Вид документа" },
    h("option", { value: "" }, "Усі види"),
    dict.document_types.map((t) => h("option", { value: t.code, selected: f.type_code === t.code }, t.name))));
  const mine = /** @type {HTMLInputElement} */ (h("input", { type: "checkbox", checked: f.mine }));
  const review = /** @type {HTMLInputElement} */ (h("input", { type: "checkbox", checked: f.needs_review }));
  const results = h("div");

  const load = async () => {
    Object.assign(f, { q: q.value.trim(), status: st.value, type_code: tp.value, mine: mine.checked, needs_review: review.checked });
    sessionStorage.setItem("sed.filters", JSON.stringify(f));
    const page = await api(`/api/v1/documents${query({
      q: f.q, status: f.status, type_code: f.type_code, mine: f.mine, needs_review: f.needs_review || null,
      limit: PAGE, offset: f.offset,
    })}`);
    renderTable(results, page, (/** @type {number} */ offset) => { f.offset = offset; load(); });
  };
  const reset = () => { f.offset = 0; load(); };
  let timer = 0;
  q.addEventListener("input", () => { clearTimeout(timer); timer = window.setTimeout(reset, 300); });
  for (const c of [st, tp, mine, review]) c.addEventListener("change", reset);

  mount(el,
    h("div", { class: "row between" },
      h("h1", null, "Документи"),
      dict.modules.ingest ? h("a", { class: "btn", href: "#/upload" }, "+ Зареєструвати документ") : null),
    h("div", { class: "card flat row" }, q, st, tp,
      h("label", { class: "check" }, mine, "Лише мої"),
      h("label", { class: "check" }, review, "Потребують перевірки ШІ")),
    results);
  await load();
}

/**
 * @param {HTMLElement} el
 * @param {{ items: any[], total: number, limit: number, offset: number }} page
 * @param {(offset: number) => void} go
 */
function renderTable(el, page, go) {
  if (!page.items.length) {
    mount(el, h("div", { class: "card empty" }, "Документів не знайдено. Змініть фільтри або зареєструйте новий документ."));
    return;
  }
  const t = threshold();
  mount(el, h("div", { class: "card table-wrap", style: "padding:0" },
    h("table", null,
      h("thead", null, h("tr", null,
        ["Заголовок", "Вид", "Статус", "Підрозділ / автор", "Реєстрація", "ШІ: реквізити", "Комплаєнс", "Змінено"].map((x) => h("th", null, x)))),
      h("tbody", null, page.items.map((d) => h("tr", { class: "clickable", onclick: () => { location.hash = `#/documents/${d.id}`; } },
        h("td", null, h("a", { href: `#/documents/${d.id}` }, d.title),
          d.access_level !== "internal" ? h("div", null, status("AccessLevel", d.access_level)) : null),
        h("td", { class: "small" }, d.document_type_name),
        h("td", null, status("DocumentStatus", d.status)),
        h("td", { class: "small" }, d.org_unit_name, h("div", { class: "muted" }, d.author_name)),
        h("td", { class: "small nowrap" }, d.registration_number ? `№ ${d.registration_number}` : "—",
          d.registration_date ? h("div", { class: "muted" }, fmtDate(d.registration_date)) : null),
        h("td", null, confidence(d.ai_confidence, t), d.needs_review ? h("div", null, pill("чекає перевірки", "warn")) : null),
        h("td", null, d.compliance_verdict ? status("ComplianceVerdict", d.compliance_verdict) : h("span", { class: "muted small" }, "не перевірявся"),
          d.open_findings ? h("div", null, pill(`${d.open_findings} відкр. зауваж.`, "danger")) : null),
        h("td", { class: "small muted nowrap" }, fmtDateTime(d.updated_at))))))),
    h("div", { class: "row between small muted" },
      `Показано ${page.offset + 1}–${page.offset + page.items.length} з ${page.total}`,
      h("div", { class: "row" },
        h("button", { type: "button", class: "small", disabled: page.offset === 0, onclick: () => go(Math.max(0, page.offset - page.limit)) }, "← Попередні"),
        h("button", { type: "button", class: "small", disabled: page.offset + page.items.length >= page.total, onclick: () => go(page.offset + page.limit) }, "Наступні →"))));

}
