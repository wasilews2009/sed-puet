// @ts-check
/**
 * Картка документа: реквізити від ШІ з ручною валідацією, нормативний аудит з цитатами,
 * маршрут погодження з візуванням і КЕП, версії, підписи, історія статусів.
 */

import { api, ApiError } from "../api.js";
import { actionButton, confidence, fmtDate, fmtDateTime, fmtSize, h, mount, notice, pill, requisiteValue, spinner, toast } from "../dom.js";
import { L, state, status, threshold } from "../state.js";

const TABS = /** @type {const} */ ([
  ["requisites", "Реквізити (ШІ)"],
  ["compliance", "Нормативний аудит"],
  ["route", "Погодження"],
  ["versions", "Версії і підписи"],
  ["history", "Історія"],
]);

/** @typedef {any} Card */

/** @param {HTMLElement} el @param {string} id @param {string} [tab] */
export async function renderCard(el, id, tab = "requisites") {
  /** @type {Card} */
  const card = await api(`/api/v1/documents/${id}`);
  let current = TABS.some(([k]) => k === tab) ? tab : "requisites";
  if (tab === "requisites" && card.permissions.my_active_step_id) current = "route";   // прийшли з черги погодження
  const reload = async () => { await renderCard(el, id, current); window.dispatchEvent(new Event("sed:counts")); };

  const body = h("div");
  const preview = h("div", { hidden: true });
  const tabs = h("div", { class: "tabs", role: "tablist" });
  const show = (/** @type {string} */ key) => {
    current = key;
    history.replaceState(null, "", `#/documents/${id}/${key}`);
    mount(tabs, TABS.map(([k, label]) => h("button", {
      type: "button", role: "tab", "aria-selected": String(k === key), class: k === key ? "active" : "",
      onclick: () => show(k),
    }, label, k === "requisites" && pendingFields(card) ? h("span", { class: "count" }, String(pendingFields(card))) : null,
    k === "route" && card.permissions.my_active_step_id ? h("span", { class: "count" }, "1") : null)));
    const render = { requisites: tabRequisites, compliance: tabCompliance, route: tabRoute, versions: tabVersions, history: tabHistory }[key];
    mount(body, h("div", { class: "empty" }, spinner()));
    Promise.resolve(render?.(body, card, reload, (/** @type {any} */ v) => showPreview(preview, card, v))).catch((e) =>
      mount(body, notice(e instanceof Error ? e.message : String(e), "danger")));
  };

  mount(el,
    header(card, () => showPreview(preview, card, card.versions.find((/** @type {any} */ v) => v.id === card.current_version_id))),
    notice(card.disclaimer),
    h("div", { class: "split", style: "margin-top:1rem" },
      h("div", { class: "card", style: "grid-column: 1 / -1" }, tabs, body), preview));
  show(current);
}

/** @param {Card} card */
function pendingFields(card) {
  return (card.extraction?.fields || []).filter((/** @type {any} */ f) => f.review_status === "pending").length;
}

/** @param {Card} card @param {() => void} onPreview */
function header(card, onPreview) {
  const item = (/** @type {string} */ k, /** @type {any} */ v) => h("div", null, h("dt", null, k), h("dd", null, v));
  return h("div", { class: "card stack" },
    h("div", { class: "row between" },
      h("div", null,
        h("div", { class: "row small" }, status("DocumentStatus", card.status), pill(card.document_type.name, "accent"),
          status("AccessLevel", card.access_level)),
        h("h1", { style: "margin-top:.4rem" }, card.title)),
      h("div", { class: "row" },
        card.current_version_id ? h("button", { type: "button", onclick: onPreview }, "Переглянути оригінал") : null,
        h("a", { class: "btn", href: "#/documents" }, "← До реєстру"))),
    card.summary ? h("p", { class: "muted", style: "margin:0" }, card.summary) : null,
    h("dl", { class: "grid kv", style: "margin:0" },
      item("Підрозділ", card.org_unit.name),
      item("Автор (зареєстрував)", card.author.name),
      item("Напрям", L("DocumentDirection", card.direction)),
      item("Реєстраційний індекс", card.registration_number ? `№ ${card.registration_number} від ${fmtDate(card.registration_date)}` : "не зареєстровано"),
      card.correspondent_name ? item("Кореспондент", `${card.correspondent_name}${card.correspondent_number ? `, № ${card.correspondent_number}` : ""}`) : null,
      card.due_date ? item("Контрольний строк", fmtDate(card.due_date)) : null,
      item("Створено", fmtDateTime(card.created_at)),
      item("Змінено", fmtDateTime(card.updated_at))));
}

/** @param {HTMLElement} el @param {Card} card @param {any} version */
async function showPreview(el, card, version) {
  if (!version) return;
  el.hidden = false;
  const grid = /** @type {HTMLElement} */ (el.parentElement?.firstElementChild);
  grid.style.removeProperty("grid-column");
  mount(el, h("div", { class: "card" }, h("div", { class: "empty" }, spinner(), " Завантаження оригіналу…")));
  const blob = await api(`/api/v1/documents/${card.id}/versions/${version.id}/file`, { blob: true });
  const url = URL.createObjectURL(blob);
  const close = () => { URL.revokeObjectURL(url); el.hidden = true; grid.style.setProperty("grid-column", "1 / -1"); };
  mount(el, h("div", { class: "card stack" },
    h("div", { class: "row between" },
      h("div", null, h("b", null, version.file_name), h("div", { class: "small muted" }, `Версія ${version.version_number} · ${fmtSize(version.size_bytes)}`)),
      h("div", { class: "row" },
        h("a", { class: "btn small", href: url, download: version.file_name }, "Завантажити"),
        h("button", { type: "button", class: "small", onclick: close }, "Закрити"))),
    version.mime_type.startsWith("image/")
      ? h("img", { src: url, alt: `Скан: ${version.file_name}`, style: "max-width:100%;border-radius:8px" })
      : h("iframe", { class: "preview", src: url, title: `Оригінал: ${version.file_name}` })));
}

// --- Реквізити ---------------------------------------------------------------------------------------

/** @param {HTMLElement} el @param {Card} card @param {() => Promise<void>} reload */
function tabRequisites(el, card, reload) {
  const x = card.extraction;
  const t = threshold();
  const parts = [];
  if (!x) {
    parts.push(notice("Реквізити ще не вилучалися для цієї версії."));
  } else {
    parts.push(h("div", { class: "row small muted" },
      `Модель: ${x.run.model}`, "·", "впевненість:", confidence(x.run.confidence, t), "·", status("HumanReviewStatus", x.run.review_status),
      x.ocr_confidence !== null ? h("span", null, "· якість розпізнавання тексту: ", confidence(x.ocr_confidence, t)) : null));
    if (x.run.status === "failed") parts.push(notice(`ШІ не вилучив реквізити: ${x.run.error_message || "модель недоступна"}. Введіть їх вручну нижче.`, "warn"));
    for (const w of x.run.warnings) parts.push(notice(w, "warn"));
    if (x.fields.length) parts.push(fieldsTable(card, x.fields, reload));
    const confident = x.fields.filter((/** @type {any} */ f) => f.review_status === "not_required" && f.extracted_value != null && f.extracted_value !== "");
    if (card.permissions.review && confident.length) {
      parts.push(h("div", { class: "row" },
        actionButton(`Перенести в картку впевнені значення (${confident.length})`, async () => {
          const r = await api(`/api/v1/documents/${card.id}/requisites/accept-confident`, { json: { row_version: card.row_version } });
          toast(`Перенесено в картку: ${r.accepted}`);
          await reload();
        }, { class: "primary small" }),
        h("span", { class: "small muted" }, "Значення з впевненістю від ", `${t * 100}%`, " можна прийняти разом; решту перевірте окремо.")));
    }
  }
  const confirmed = Object.entries(card.requisites || {});
  parts.push(h("h3", { style: "margin-top:1.2rem" }, "Реквізити картки (підтверджені людиною)"),
    confirmed.length
      ? h("dl", { class: "grid kv" }, confirmed.map(([k, v]) => h("div", null, h("dt", null, state.dict?.field_labels[k] || k), h("dd", null, fieldValue(k, v)))))
      : h("p", { class: "muted" }, "Поки жодного: значення ШІ стають реквізитами картки лише після підтвердження."));
  if (card.permissions.review && (!x || x.run.status === "failed" || x.run.review_status === "pending")) parts.push(manualForm(card, reload));
  mount(el, h("div", { class: "stack" }, parts));
}

/** @param {Card} card @param {any[]} fields @param {() => Promise<void>} reload */
function fieldsTable(card, fields, reload) {
  const t = threshold();
  return h("div", { class: "table-wrap" }, h("table", null,
    h("thead", null, h("tr", null, ["Реквізит", "Значення від ШІ", "Впевненість", "Перевірка", ""].map((c) => h("th", null, c)))),
    h("tbody", null, fields.map((f) => {
      const actions = h("td", { class: "nowrap" });
      const editorSlot = h("div");
      if (card.permissions.review && f.review_status !== "pending") {
        mount(actions, h("button", { type: "button", class: "small", onclick: () => mount(editorSlot, correctEditor(card, f, reload, () => mount(editorSlot))) }, "Виправити"));
      }
      if (card.permissions.review && f.review_status === "pending") {
        mount(actions,
          actionButton("Підтвердити", () => review(card, f, { decision: "confirm" }, reload), { class: "small ok" }),
          " ",
          h("button", { type: "button", class: "small", onclick: () => mount(editorSlot, correctEditor(card, f, reload, () => mount(editorSlot))) }, "Виправити"),
          " ",
          h("button", { type: "button", class: "small danger", onclick: () => mount(editorSlot, rejectEditor(card, f, reload, () => mount(editorSlot))) }, "Відхилити"));
      }
      return h("tr", null,
        h("td", null, h("b", null, f.label), f.page_no ? h("div", { class: "small muted" }, `стор. ${f.page_no}`) : null),
        h("td", null, fieldValue(f.key, f.extracted_value),
          f.review_status === "corrected" ? h("div", { class: "small" }, "Виправлено: ", h("b", null, fieldValue(f.key, f.validated_value))) : null,
          f.issues.length ? h("ul", { class: "small muted", style: "margin:.3rem 0 0;padding-left:1rem" }, f.issues.map((/** @type {string} */ i) => h("li", null, i))) : null,
          editorSlot),
        h("td", null, confidence(f.confidence, t)),
        h("td", null, status("HumanReviewStatus", f.review_status),
          f.reviewed_by ? h("div", { class: "small muted" }, `${f.reviewed_by.name}, ${fmtDateTime(f.reviewed_at)}`) : null,
          f.review_comment ? h("div", { class: "small" }, f.review_comment) : null),
        actions);
    }))));
}

/** Вид документа ШІ повертає кодом (memo, order…) — показуємо назву. @param {string} key @param {any} v */
function fieldValue(key, v) {
  return key === "document_type" && typeof v === "string" ? L("DocumentKind", v) : requisiteValue(v);
}

/** @param {Card} card @param {any} f @param {any} body @param {() => Promise<void>} reload */
async function review(card, f, body, reload) {
  try {
    await api(`/api/v1/documents/${card.id}/fields/${f.id}/review`, { json: { ...body, row_version: card.row_version } });
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) { toast(e.message, "danger"); await reload(); return; }
    throw e;
  }
  toast(`«${f.label}»: ${{ confirm: "підтверджено", correct: "виправлено", reject: "відхилено" }[/** @type {"confirm"} */ (body.decision)]}`);
  await reload();
}

/** Поля редактора залежно від реквізиту. @param {string} key @param {any} value */
function valueInputs(key, value) {
  if (key === "document_date") {
    const input = /** @type {HTMLInputElement} */ (h("input", { type: "date", value: /^\d{4}-\d{2}-\d{2}$/.test(String(value || "")) ? value : "" }));
    return { node: input, read: () => input.value };
  }
  if (key === "author") {
    const v = value && typeof value === "object" ? value : {};
    const name = /** @type {HTMLInputElement} */ (h("input", { placeholder: "Ім'я та прізвище", value: v.full_name || "" }));
    const pos = /** @type {HTMLInputElement} */ (h("input", { placeholder: "Посада", value: v.position || "" }));
    const unit = /** @type {HTMLInputElement} */ (h("input", { placeholder: "Підрозділ / організація", value: v.org_unit || "" }));
    return { node: h("div", { class: "grid" }, name, pos, unit), read: () => ({ full_name: name.value, position: pos.value || null, org_unit: unit.value || null }) };
  }
  if (key === "interested_parties") {
    const lines = (Array.isArray(value) ? value : []).map((/** @type {any} */ p) => [p.full_name, p.position].filter(Boolean).join(", "));
    const ta = /** @type {HTMLTextAreaElement} */ (h("textarea", { placeholder: "Одна особа в рядку: ПІБ, посада" }));
    ta.value = lines.join("\n");
    return {
      node: ta,
      read: () => ta.value.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
        const [full_name, ...rest] = l.split(",");
        return { full_name: full_name.trim(), position: rest.join(",").trim() || null };
      }),
    };
  }
  const ta = /** @type {HTMLTextAreaElement} */ (h("textarea", { rows: key === "summary" ? 4 : 2 }));
  ta.value = typeof value === "string" ? value : "";
  return { node: ta, read: () => ta.value };
}

/** @param {Card} card @param {any} f @param {() => Promise<void>} reload @param {() => void} close */
function correctEditor(card, f, reload, close) {
  const inp = valueInputs(f.key, f.extracted_value);
  const comment = /** @type {HTMLInputElement} */ (h("input", { placeholder: "Коментар (необов'язково)" }));
  return h("div", { class: "editor" }, inp.node, comment, h("div", { class: "row" },
    actionButton("Зберегти виправлення", () => review(card, f, { decision: "correct", value: inp.read(), comment: comment.value || null }, reload), { class: "small primary" }),
    h("button", { type: "button", class: "small", onclick: close }, "Скасувати")));
}

/** @param {Card} card @param {any} f @param {() => Promise<void>} reload @param {() => void} close */
function rejectEditor(card, f, reload, close) {
  const comment = /** @type {HTMLInputElement} */ (h("input", { placeholder: "Чому значення хибне (обов'язково)", required: true }));
  return h("div", { class: "editor" }, comment, h("div", { class: "row" },
    actionButton("Відхилити значення", async () => {
      if (!comment.value.trim()) throw new Error("Вкажіть причину відхилення");
      await review(card, f, { decision: "reject", comment: comment.value }, reload);
    }, { class: "small danger" }),
    h("button", { type: "button", class: "small", onclick: close }, "Скасувати")));
}

/** Ручне введення реквізитів (ШІ не впорався). @param {Card} card @param {() => Promise<void>} reload */
function manualForm(card, reload) {
  const keys = ["document_date", "registration_number", "title", "summary", "author"];
  const inputs = keys.map((k) => ({ key: k, ...valueInputs(k, card.requisites?.[k]) }));
  return h("details", { class: "card flat", open: card.extraction?.run.status === "failed" || !card.extraction },
    h("summary", null, h("b", null, "Ввести реквізити вручну")),
    h("div", { class: "stack", style: "margin-top:.8rem" },
      inputs.map((i) => h("label", { class: "field" }, h("span", null, state.dict?.field_labels[i.key] || i.key), i.node)),
      actionButton("Зберегти реквізити", async () => {
        /** @type {Record<string, any>} */
        const values = {};
        for (const i of inputs) {
          const v = /** @type {any} */ (i.read());
          if (v && (typeof v !== "object" || v.full_name)) values[i.key] = v;
        }
        await api(`/api/v1/documents/${card.id}/requisites`, { method: "PUT", json: { values, row_version: card.row_version } });
        toast("Реквізити збережено");
        await reload();
      }, { class: "primary" })));
}

// --- Нормативний аудит ------------------------------------------------------------------------------

/** @param {HTMLElement} el @param {Card} card @param {() => Promise<void>} reload */
function tabCompliance(el, card, reload) {
  if (!state.dict?.modules.compliance) {
    mount(el, notice("Модуль нормативного аудиту не підключено на цьому стенді (потрібні Qdrant, сервер ембедингів і LLM)."));
    return;
  }
  const c = card.compliance;
  const runBtn = card.permissions.check_compliance
    ? actionButton(c ? "Перевірити повторно" : "Перевірити на відповідність нормам", async () => {
      toast("Перевірка триває: пошук норм у Статуті, положеннях ПУЕТ і наказах МОН, оцінка кожного пункту…", "");
      await api(`/api/v1/documents/${card.id}/compliance-checks`, { json: {} });
      toast("Перевірку завершено");
      await reload();
    }, { class: c ? "" : "primary" })
    : null;
  if (!c) {
    mount(el, h("div", { class: "stack" },
      h("p", null, "Перевірка шукає пункти Статуту ПУЕТ, положень університету, законів і наказів МОН, яким може суперечити проєкт, і наводить дослівні цитати норм."),
      card.versions.length && !card.versions[0].has_text ? notice("У версії немає розпізнаного тексту — перевірка неможлива.", "warn") : null,
      runBtn));
    return;
  }
  const t = threshold();
  const verdictTone = { compliant: "ok", issues_found: "warn", non_compliant: "danger", inconclusive: "warn" }[/** @type {"compliant"} */ (c.verdict)];
  const openCount = c.findings.filter((/** @type {any} */ f) => f.status === "open").length;
  mount(el, h("div", { class: "stack" },
    h("div", { class: `notice ${verdictTone}` }, h("b", null, L("ComplianceVerdict", c.verdict)), c.summary ? ` — ${c.summary}` : ""),
    h("div", { class: "row small muted" },
      `Норми на ${fmtDate(c.regulations_as_of)}`, "·", `актів розглянуто: ${c.sources_considered}`, "·",
      `модель ${c.run.model}`, "·", "впевненість:", confidence(c.run.confidence, t), "·", status("HumanReviewStatus", c.run.review_status),
      "·", fmtDateTime(c.created_at)),
    c.document_version_id !== card.current_version_id ? notice("Звіт стосується попередньої версії документа.", "warn") : null,
    c.review_reasons.length ? h("div", null, h("b", null, "Чому потрібна перевірка людиною:"), h("ul", null, c.review_reasons.map((/** @type {string} */ r) => h("li", null, r)))) : null,
    c.run.warnings.map((/** @type {string} */ w) => notice(w, "warn")),
    c.findings.length ? c.findings.map((/** @type {any} */ f) => finding(card, f, reload)) : h("p", { class: "muted" }, "Зауважень немає."),
    h("div", { class: "row" }, runBtn,
      card.permissions.review && c.run.review_status === "pending" ? [
        actionButton("Підтвердити звіт", async () => {
          if (openCount) throw new Error("Спершу розгляньте всі відкриті зауваження");
          await api(`/api/v1/documents/${card.id}/ai-runs/${c.run.id}/review`, { json: { decision: "confirm" } });
          await reload();
        }, { class: "ok" }),
        actionButton("Відхилити звіт", async () => {
          const reason = prompt("Чому звіт ШІ відхилено?");
          if (!reason) return;
          await api(`/api/v1/documents/${card.id}/ai-runs/${c.run.id}/review`, { json: { decision: "reject", comment: reason } });
          await reload();
        }, { class: "danger" })] : null)));
}

/** @param {Card} card @param {any} f @param {() => Promise<void>} reload */
function finding(card, f, reload) {
  const t = threshold();
  const resolve = (/** @type {string} */ st, /** @type {string | null} */ comment) =>
    api(`/api/v1/documents/${card.id}/findings/${f.id}/resolution`, { json: { status: st, comment } }).then(reload);
  return h("div", { class: `finding ${f.severity}` },
    h("div", { class: "row between" },
      h("div", { class: "row" }, status("FindingSeverity", f.severity), pill(L("FindingCategory", f.category)), h("b", null, f.title)),
      h("div", { class: "row small" }, confidence(f.confidence, t), status("FindingStatus", f.status))),
    h("p", { style: "margin:.5rem 0" }, f.explanation),
    f.fragment_text ? h("div", { class: "small" }, "Фрагмент проєкту: ", h("span", { class: "fragment" }, f.fragment_text)) : null,
    f.recommendation ? h("p", { class: "small", style: "margin:.4rem 0" }, h("b", null, "Рекомендація: "), f.recommendation) : null,
    f.citations.map((/** @type {any} */ c) => h("div", { class: "small", style: "margin-top:.4rem" },
      h("b", null, c.citation_label), " ",
      c.quote_verified ? pill("цитату звірено з текстом акта", "ok") : pill("цитату не знайдено дослівно", "warn"),
      c.source_status !== "in_force" ? [" ", pill(`акт: ${c.source_status}`, "danger")] : null,
      h("blockquote", null, c.quoted_text))),
    f.status !== "open"
      ? h("div", { class: "small muted", style: "margin-top:.4rem" },
        `${L("FindingStatus", f.status)}: ${f.resolved_by?.name || ""}, ${fmtDateTime(f.resolved_at)}`, f.resolution_comment ? ` — ${f.resolution_comment}` : "")
      : card.permissions.review ? h("div", { class: "row", style: "margin-top:.6rem" },
        actionButton("Прийняти до виправлення", () => resolve("accepted", null), { class: "small" }),
        actionButton("Виправлено", () => resolve("fixed", null), { class: "small ok" }),
        actionButton("Хибне спрацювання", async () => {
          const reason = prompt("Чому зауваження хибне? (буде збережено в журналі)");
          if (reason) await resolve("dismissed", reason);
        }, { class: "small danger" })) : null);
}

// --- Погодження -----------------------------------------------------------------------------------

/** @param {HTMLElement} el @param {Card} card @param {() => Promise<void>} reload */
async function tabRoute(el, card, reload) {
  if (!state.dict?.modules.routing) {
    mount(el, notice("Модуль маршрутів погодження не підключено на цьому стенді."));
    return;
  }
  const r = card.route;
  const parts = [];
  /** @type {Record<string, string>} */
  const titles = {};
  if (r) {
    try {
      const view = await api(`/api/v1/documents/${card.id}/route`);
      for (const s of view.steps || []) titles[s.id] = s.title_uk;
    } catch { /* назви етапів — лише підказка; без них маршрут показується за видом кроку */ }
  }
  const myStep = r?.steps.find((/** @type {any} */ s) => s.id === card.permissions.my_active_step_id);
  if (myStep) parts.push(decisionPanel(card, myStep, reload));
  if (r) {
    parts.push(h("div", { class: "row between" },
      h("div", null, h("h3", null, r.template_name || "Маршрут погодження"),
        h("div", { class: "row small muted" }, status("RouteStatus", r.status), `ініціатор: ${r.initiated_by.name}`,
          r.started_at ? `· запущено ${fmtDateTime(r.started_at)}` : "", r.completed_at ? `· завершено ${fmtDateTime(r.completed_at)}` : "")),
      r.status === "active" && (card.author.id === state.me?.id || r.initiated_by.id === state.me?.id)
        ? actionButton("Скасувати маршрут", async () => {
          const reason = prompt("Причина скасування маршруту");
          if (!reason || reason.trim().length < 3) return;
          await api(`/api/v1/routes/${r.id}/cancel`, { json: { reason } });
          await reload();
        }, { class: "small danger" }) : null),
    r.cancel_reason ? notice(`Скасовано: ${r.cancel_reason}`, "warn") : null,
    h("ol", { class: "timeline" }, r.steps.map((/** @type {any} */ s) => {
      const sig = card.signatures.find((/** @type {any} */ x) => x.approval_step_id === s.id);
      return h("li", { class: s.status },
        h("div", { class: "row" }, h("b", null, s.assignee.name), s.acting_role ? h("span", { class: "muted small" }, L("UserRole", s.acting_role)) : null,
          status("StepStatus", s.status), s.requires_kep ? pill("КЕП", "accent") : null, s.is_overdue ? pill("прострочено", "danger") : null,
          s.is_mandatory ? null : pill("необов'язковий")),
        titles[s.id] ? h("div", null, titles[s.id]) : null,
        h("div", { class: "small muted" }, `Етап ${s.stage_no} · ${L("StepKind", s.step_kind)}`,
          s.original_assignee ? ` · замість ${s.original_assignee.name}` : "",
          s.activated_at ? ` · отримано ${fmtDateTime(s.activated_at)}` : "", s.due_at ? ` · строк ${fmtDateTime(s.due_at)}` : "",
          s.decided_at ? ` · рішення ${fmtDateTime(s.decided_at)}` : ""),
        s.decision_comment ? h("blockquote", null, s.decision_comment) : null,
        sig ? h("div", { class: "small" }, "КЕП: ", sig.cert_subject_cn, " · ", status("SignatureValidation", sig.validation_status), ` · ${fmtDateTime(sig.signed_at)}`) : null);
    })));
  } else {
    parts.push(h("p", { class: "muted" }, "Маршрут погодження ще не сформовано."));
  }
  if (card.permissions.start_route) parts.push(startRoute(card, reload));
  mount(el, h("div", { class: "stack" }, parts));
}

/** @param {Card} card @param {() => Promise<void>} reload */
function startRoute(card, reload) {
  const slot = h("div");
  const ack = /** @type {HTMLInputElement} */ (h("input", { type: "checkbox" }));
  return h("div", { class: "card flat stack" },
    h("h3", null, card.route ? "Повторне погодження" : "Погодження документа"),
    h("p", { class: "small muted", style: "margin:0" }, "Учасників визначає типовий маршрут для виду документа і підрозділу: наступний учасник отримує документ лише після позитивної візи попереднього."),
    h("div", { class: "row" },
      actionButton("Сформувати маршрут", async () => {
        const plan = await api(`/api/v1/documents/${card.id}/route-preview`, { json: {} });
        mount(slot, h("div", { class: "stack" },
          h("b", null, plan.template_name),
          h("ol", null, plan.steps.map((/** @type {any} */ s) => h("li", null,
            `${s.title_uk}: `, h("b", null, s.assignee_name), s.requires_kep ? " (КЕП)" : "",
            s.will_skip ? h("span", { class: "muted" }, ` — буде пропущено: ${s.skip_reason}`) : null))),
          h("label", { class: "check small" }, ack, "Запустити попри відкриті критичні зауваження нормативного аудиту (рішення фіксується в журналі)"),
          actionButton("Запустити погодження", async () => {
            await api(`/api/v1/documents/${card.id}/routes`, { json: { acknowledge_findings: ack.checked } });
            toast("Маршрут запущено: документ надіслано першому учаснику");
            await reload();
          }, { class: "primary" })));
      })),
    slot);
}

/** @param {Card} card @param {any} step @param {() => Promise<void>} reload */
function decisionPanel(card, step, reload) {
  const comment = /** @type {HTMLTextAreaElement} */ (h("textarea", { placeholder: "Зауваження (обов'язкові для «із зауваженнями» і для відхилення)" }));
  const withRemarks = () => (comment.value.trim() ? "approved_with_remarks" : "approved");
  const sigFile = /** @type {HTMLInputElement} */ (h("input", { type: "file", accept: ".p7s,.sig,.p7m,application/pkcs7-signature" }));
  const reqInfo = h("div", { class: "small muted" });
  const done = async (/** @type {string} */ msg) => { toast(msg); await reload(); };

  /** @param {string} signatureBase64 */
  const sign = async (signatureBase64) => {
    await api(`/api/v1/steps/${step.id}/sign`, {
      json: { signature_base64: signatureBase64, provider: "file_key", decision: withRemarks(), comment: comment.value.trim() || null },
    });
    await done("Візу з КЕП накладено. Документ передано наступному учаснику");
  };

  if (step.requires_kep) {
    api(`/api/v1/steps/${step.id}/signing-request`).then((r) => mount(reqInfo,
      "Підписується файл ", h("b", null, r.file_name), ", SHA-256: ", h("span", { class: "mono" }, r.content_sha256),
      `. Формат: ${r.signature_container}.`)).catch(() => mount(reqInfo));
  }

  return h("div", { class: "card stack", style: "border-color:var(--accent)" },
    h("h3", null, `Ваше рішення: ${L("StepKind", step.step_kind).toLowerCase()}`),
    h("p", { class: "small muted", style: "margin:0" },
      "Перегляньте оригінал, реквізити і звіт нормативного аудиту. Рішення приймаєте ви: ШІ лише підказує."),
    comment,
    step.requires_kep ? h("div", { class: "stack" },
      reqInfo,
      state.dict?.modules.kep_mock ? h("div", { class: "row" },
        actionButton("Погодити і підписати тестовим ключем", async () => {
          const mock = await api(`/api/v1/dev/steps/${step.id}/mock-signature`, { json: {} });
          await sign(mock.signature_base64);
        }, { class: "ok" }),
        h("span", { class: "small muted" }, "Імітація КЕП стенду: без юридичної сили")) : null,
      h("div", { class: "row" },
        h("label", { class: "field" }, h("span", null, "Файл підпису (відокремлений CAdES з «ІІТ Користувач ЦСК» або Дія.Підпис)"), sigFile),
        actionButton("Надіслати підпис", async () => {
          const f = sigFile.files?.[0];
          if (!f) throw new Error("Оберіть файл підпису .p7s");
          const bytes = new Uint8Array(await f.arrayBuffer());
          let bin = "";
          for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
          await sign(btoa(bin));
        }, { class: "primary" })))
      : h("div", { class: "row" },
        actionButton("Погодити", async () => {
          await api(`/api/v1/steps/${step.id}/decision`, { json: { decision: withRemarks(), comment: comment.value.trim() || null } });
          await done("Рішення збережено");
        }, { class: "ok" })),
    h("div", { class: "row" },
      actionButton("Відхилити (повернути на доопрацювання)", async () => {
        if (!comment.value.trim()) throw new Error("Вкажіть причину відхилення в полі зауважень");
        await api(`/api/v1/steps/${step.id}/decision`, { json: { decision: "rejected", comment: comment.value.trim() } });
        await done("Документ повернуто на доопрацювання");
      }, { class: "danger" })));
}

// --- Версії, підписи, історія ---------------------------------------------------------------------------

/** @param {HTMLElement} el @param {Card} card @param {() => Promise<void>} _reload @param {(v: any) => void} preview */
function tabVersions(el, card, _reload, preview) {
  mount(el, h("div", { class: "stack" },
    h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, ["№", "Файл", "Джерело", "Створено", "SHA-256", ""].map((c) => h("th", null, c)))),
      h("tbody", null, card.versions.map((/** @type {any} */ v) => h("tr", null,
        h("td", null, String(v.version_number), v.id === card.current_version_id ? h("div", null, pill("поточна", "accent")) : null),
        h("td", null, v.file_name, h("div", { class: "small muted" }, `${fmtSize(v.size_bytes)}${v.page_count ? `, ${v.page_count} стор.` : ""}`),
          v.is_locked ? h("div", null, pill("заблоковано для змін", "")) : null),
        h("td", { class: "small" }, { upload: "Завантаження", editor: "Редактор", template: "Шаблон", ai_generated: "Згенеровано ШІ", ocr_scan: "Скан (OCR)" }[/** @type {"upload"} */ (v.source)] || v.source),
        h("td", { class: "small" }, fmtDateTime(v.created_at), h("div", { class: "muted" }, v.created_by.name)),
        h("td", { class: "mono small" }, v.content_sha256),
        h("td", null, h("button", { type: "button", class: "small", onclick: () => preview(v) }, "Переглянути"))))))),
    h("h3", null, "Кваліфіковані електронні підписи"),
    card.signatures.length ? h("div", { class: "table-wrap" }, h("table", null,
      h("thead", null, h("tr", null, ["Підписувач", "Сертифікат", "Формат", "Час підпису", "Статус"].map((c) => h("th", null, c)))),
      h("tbody", null, card.signatures.map((/** @type {any} */ s) => h("tr", null,
        h("td", null, s.signer.name, h("div", { class: "small muted" }, s.cert_subject_cn)),
        h("td", { class: "small" }, s.cert_issuer, h("div", { class: "mono muted" }, s.cert_serial)),
        h("td", { class: "small" }, s.signature_format),
        h("td", { class: "small" }, fmtDateTime(s.signed_at)),
        h("td", null, status("SignatureValidation", s.validation_status))))))) : h("p", { class: "muted" }, "Підписів ще немає.")));
}

/** @param {HTMLElement} el @param {Card} card */
function tabHistory(el, card) {
  mount(el, card.history.length ? h("ol", { class: "timeline" }, card.history.map((/** @type {any} */ x) => h("li", { class: x.to_status === "cancelled" ? "rejected" : "approved" },
    h("div", { class: "row" }, x.from_status ? [status("DocumentStatus", x.from_status), "→"] : null, status("DocumentStatus", x.to_status)),
    h("div", { class: "small muted" }, fmtDateTime(x.changed_at), " · ",
      x.changed_by ? x.changed_by.name : { system: "система", ai_service: "ШІ-сервіс", user: "користувач" }[/** @type {"system"} */ (x.actor_type)] || x.actor_type,
      x.reason ? ` · ${x.reason}` : ""))))
    : h("p", { class: "muted" }, "Змін статусу ще не було."));
}
