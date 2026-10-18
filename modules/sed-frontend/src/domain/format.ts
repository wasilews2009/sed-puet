/** Форматування дат, розмірів і впевненості для українського інтерфейсу. */

const dateFmt = new Intl.DateTimeFormat("uk-UA", { day: "2-digit", month: "2-digit", year: "numeric" });
const dateTimeFmt = new Intl.DateTimeFormat("uk-UA", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

function parse(v: string | null | undefined): Date | null {
  if (!v) return null;
  // дата без часу (YYYY-MM-DD) — локальна, а не UTC, щоб не з'їжджала на добу
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
  const d = m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function fmtDate(v: string | null | undefined): string {
  const d = parse(v);
  return d ? dateFmt.format(d) : "—";
}

export function fmtDateTime(v: string | null | undefined): string {
  const d = parse(v);
  return d ? dateTimeFmt.format(d) : "—";
}

export function fmtSize(bytes: number | null | undefined): string {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
}

/** Впевненість ШІ приходить рядком-десятковим ("0.912"); повертає частку 0..1 або null. */
export function toScore(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : null;
}

export function fmtPercent(v: number | null): string {
  return v === null ? "—" : `${Math.round(v * 100)}%`;
}

/** Дні до дати (від'ємні — уже минула). */
export function daysUntil(v: string, now = new Date()): number {
  const d = parse(v);
  if (!d) return Number.NaN;
  return Math.floor((d.getTime() - now.getTime()) / 86_400_000);
}

export function shortHash(hex: string, n = 12): string {
  return hex.length > n * 2 ? `${hex.slice(0, n)}…${hex.slice(-n)}` : hex;
}
