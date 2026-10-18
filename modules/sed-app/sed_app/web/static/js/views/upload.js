// @ts-check
/** Реєстрація вхідного/внутрішнього документа: файл → MinIO → OCR → реквізити ШІ (модуль sed_ingest). */

import { api } from "../api.js";
import { confidence, h, mount, notice, spinner } from "../dom.js";
import { L, state, status, threshold } from "../state.js";

const ACCEPT = "application/pdf,image/png,image/jpeg,image/tiff,image/webp,.pdf,.png,.jpg,.jpeg,.tif,.tiff,.webp";

/** @param {HTMLElement} el */
export async function renderUpload(el) {
  const me = /** @type {import("../state.js").Me} */ (state.me);
  const dict = /** @type {import("../state.js").Dictionaries} */ (state.dict);
  const units = me.permissions.upload_all_units
    ? dict.org_units
    : dict.org_units.filter((u) => me.permissions.upload_unit_ids.includes(u.id));
  if (!units.length) {
    mount(el, h("h1", null, "Реєстрація документа"),
      notice("У вас немає підрозділу, у якому можна реєструвати документи. Зверніться до адміністратора.", "warn"));
    return;
  }

  const file = /** @type {HTMLInputElement} */ (h("input", { type: "file", name: "file", accept: ACCEPT, required: true }));
  const unit = /** @type {HTMLSelectElement} */ (h("select", { name: "org_unit_id", required: true },
    units.map((u) => h("option", { value: u.id, selected: u.id === me.primary_org_unit_id }, u.name))));
  const direction = /** @type {HTMLSelectElement} */ (h("select", { name: "direction" },
    h("option", { value: "" }, "Визначити за видом документа"),
    Object.entries(dict.labels.DocumentDirection).map(([v, l]) => h("option", { value: v }, l))));
  const access = /** @type {HTMLSelectElement} */ (h("select", { name: "access_level" },
    h("option", { value: "" }, "За видом документа (посилюється, якщо знайдено персональні дані)"),
    Object.entries(dict.labels.AccessLevel).map(([v, l]) => h("option", { value: v }, l))));
  const corrField = h("label", { class: "field", hidden: true }, h("span", null, "Кореспондент (обов'язково для вхідних)"),
    h("input", { name: "correspondent_name", maxlength: 512, placeholder: "Міністерство освіти і науки України" }));
  direction.addEventListener("change", () => { corrField.hidden = direction.value !== "incoming"; });

  const result = h("div");
  const submit = /** @type {HTMLButtonElement} */ (h("button", { type: "submit", class: "primary" }, "Зареєструвати і розпізнати"));
  const form = /** @type {HTMLFormElement} */ (h("form", { class: "form card" },
    h("label", { class: "field" }, h("span", null, "Файл документа (PDF або скан/фото до 50 МБ)"), file),
    h("label", { class: "field" }, h("span", null, "Підрозділ реєстрації"), unit),
    h("div", { class: "grid" },
      h("label", { class: "field" }, h("span", null, "Вид кореспонденції"), direction),
      h("label", { class: "field" }, h("span", null, "Гриф доступу"), access)),
    corrField,
    h("div", { class: "row" }, submit,
      h("span", { class: "muted small" }, "Файл зберігається в сховищі університету; текст розпізнається локально, дані не залишають периметр.")),
  ));

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    for (const k of ["direction", "access_level", "correspondent_name"]) if (!fd.get(k)) fd.delete(k);
    submit.disabled = true;
    mount(result, h("div", { class: "card row" }, spinner(),
      "Зберігаємо оригінал, розпізнаємо текст і вилучаємо реквізити за ДСТУ 4163:2020. Це може тривати кілька хвилин…"));
    try {
      const r = await api("/api/v1/documents/ingest", { form: fd });
      renderResult(result, r);
      window.dispatchEvent(new Event("sed:counts"));
      form.reset();
    } catch (e) {
      mount(result, notice(e instanceof Error ? e.message : String(e), "danger"));
    } finally {
      submit.disabled = false;
    }
  });

  mount(el, h("h1", null, "Реєстрація документа"),
    h("p", { class: "muted" }, "ШІ визначить вид документа і вилучить реквізити. Значення з впевненістю нижче ",
      `${threshold() * 100}%`, " потраплять у чергу ручної перевірки — без вашого підтвердження вони не стануть реквізитами картки."),
    form, result);
}

/** @param {HTMLElement} el @param {any} r IngestResponse */
function renderResult(el, r) {
  const tone = r.status === "processed" ? "ok" : "warn";
  const headline = {
    processed: "Документ зареєстровано, реквізити вилучено з достатньою впевненістю.",
    needs_review: "Документ зареєстровано. Частина реквізитів потребує вашої перевірки.",
    manual_entry_required: "Документ зареєстровано, але реквізити не вилучено: введіть їх вручну в картці.",
  }[/** @type {"processed" | "needs_review" | "manual_entry_required"} */ (r.status)];
  mount(el, h("div", { class: "card stack" },
    h("div", { class: `notice ${tone}` }, headline),
    h("dl", { class: "grid kv" },
      h("div", null, h("dt", null, "Вид"), h("dd", null,
        state.dict?.document_types.find((t) => t.code === r.document_type_code)?.name ?? r.document_type_code)),
      h("div", null, h("dt", null, "Гриф"), h("dd", null, status("AccessLevel", r.access_level))),
      h("div", null, h("dt", null, "Напрям"), h("dd", null, L("DocumentDirection", r.direction))),
      h("div", null, h("dt", null, "Впевненість ШІ"), h("dd", null, confidence(r.confidence, threshold()))),
      h("div", null, h("dt", null, "Розпізнавання"), h("dd", null,
        `${r.ocr.pages_processed} із ${r.ocr.page_count} стор., ${r.ocr.used_ocr ? "OCR" : "текстовий шар PDF"}`))),
    r.review_reasons?.length ? h("div", null, h("b", null, "Що перевірити: "), h("ul", null, r.review_reasons.map((/** @type {string} */ x) => h("li", null, x)))) : null,
    r.warnings?.length ? notice(r.warnings.join(" "), "warn") : null,
    r.duplicate_of ? notice("Такий самий файл уже зареєстровано раніше.", "warn") : null,
    h("div", { class: "row" },
      h("a", { class: "btn primary", href: `#/documents/${r.document_id}` }, "Відкрити картку"),
      r.duplicate_of ? h("a", { class: "btn", href: `#/documents/${r.duplicate_of}` }, "Відкрити попередній документ") : null)));
}
