/** Подання реквізитів (ДСТУ 4163:2020), які ШІ повертає як рядки, дати, особу або список осіб. */

export interface Person {
  full_name: string;
  position?: string | null;
  org_unit?: string | null;
}

export function isPerson(v: unknown): v is Person {
  return !!v && typeof v === "object" && typeof (v as Person).full_name === "string";
}

/** Особа від ШІ може прийти без ПІБ (розпізнано лише посаду) — показуємо, що є. */
function looksLikePerson(v: object): v is { full_name?: string | null; position?: string | null; org_unit?: string | null } {
  return "full_name" in v || "position" in v;
}

export function requisiteText(v: unknown, kindLabel?: (code: string) => string): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "string") {
    if (/^\d{4}-\d{2}-\d{2}$/.test(v)) {
      const [y, m, d] = v.split("-");
      return `${d}.${m}.${y}`;
    }
    return kindLabel ? kindLabel(v) : v;
  }
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) return v.map((x) => requisiteText(x)).join("; ") || "—";
  if (looksLikePerson(v)) {
    return [v.full_name || "ПІБ не розпізнано", v.position, v.org_unit].filter(Boolean).join(", ");
  }
  return JSON.stringify(v);
}

/** Рядки «ПІБ, посада» → список зацікавлених осіб. */
export function parsePeople(text: string): Person[] {
  return text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => {
      const [name, ...rest] = l.split(",");
      return { full_name: (name ?? "").trim(), position: rest.join(",").trim() || null };
    });
}

export function peopleText(v: unknown): string {
  return (Array.isArray(v) ? v : [])
    .filter(isPerson)
    .map((p) => [p.full_name, p.position].filter(Boolean).join(", "))
    .join("\n");
}

/** Поля ручного введення, коли ШІ не впорався. */
export const MANUAL_KEYS = ["document_date", "registration_number", "title", "summary", "author"] as const;
