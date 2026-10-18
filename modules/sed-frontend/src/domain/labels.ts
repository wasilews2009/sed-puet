/**
 * Підписи значень перелічень українською. Основне джерело — `/dictionaries` (labels), щоб назви
 * статусів не розходились з бекендом; тут лише тони (кольори) і запасні підписи для перелічень,
 * яких немає в довіднику.
 */
import type { Dictionaries } from "../api/types";

export type Tone = "neutral" | "ok" | "warn" | "danger" | "accent";

const TONES: Record<string, Record<string, Tone>> = {
  DocumentStatus: {
    draft: "neutral",
    ai_review: "accent",
    on_approval: "accent",
    revision_required: "warn",
    approved: "ok",
    signed: "ok",
    registered: "ok",
    in_execution: "accent",
    executed: "ok",
    archived: "neutral",
    cancelled: "danger",
  },
  StepStatus: {
    pending: "neutral",
    active: "accent",
    approved: "ok",
    approved_with_remarks: "warn",
    rejected: "danger",
    expired: "danger",
    skipped: "neutral",
    cancelled: "neutral",
  },
  RouteStatus: { draft: "neutral", active: "accent", completed: "ok", rejected: "danger", cancelled: "danger" },
  HumanReviewStatus: { not_required: "neutral", pending: "warn", confirmed: "ok", corrected: "ok", rejected: "danger" },
  ComplianceVerdict: { compliant: "ok", issues_found: "warn", non_compliant: "danger", inconclusive: "warn" },
  FindingSeverity: { critical: "danger", major: "warn", minor: "accent" },
  FindingStatus: { open: "warn", accepted: "accent", dismissed: "neutral", fixed: "ok" },
  SignatureValidation: {
    valid: "ok",
    invalid: "danger",
    cert_revoked: "danger",
    cert_expired: "danger",
    indeterminate: "warn",
    pending: "neutral",
  },
  AccessLevel: { public: "ok", internal: "neutral", restricted: "warn", personal_data: "danger" },
  RegulatoryStatus: { in_force: "ok", draft: "neutral", amended: "warn", repealed: "danger" },
  Coverage: { strong: "ok", weak: "warn", none: "danger" },
};

const FALLBACK: Record<string, Record<string, string>> = {
  RegulatoryLevel: {
    law: "Закон",
    cabinet: "Постанова КМУ",
    ministry: "Наказ МОН",
    standard: "Стандарт (ДСТУ)",
    university: "Акт університету",
    unit: "Акт підрозділу",
  },
  RegulatoryStatus: { draft: "Проєкт", in_force: "Чинний", amended: "Змінений", repealed: "Втратив чинність" },
  Coverage: { strong: "Норми знайдено", weak: "Норми знайдено частково", none: "Норм не знайдено" },
  VersionSource: {
    upload: "Завантаження",
    editor: "Редактор",
    template: "Шаблон",
    ai_generated: "Згенеровано ШІ",
    ocr_scan: "Скан (OCR)",
  },
  ActorType: { system: "система", ai_service: "ШІ-сервіс", user: "користувач" },
  KeyFormat: {
    jks: "Файловий ключ JKS (ПриватБанк)",
    pkcs12: "PKCS#12 (.pfx/.p12)",
    key6_dat: "Key-6.dat (ІІТ / Дія)",
    token: "Захищений носій (токен)",
    cloud: "Хмарний КЕП (Дія.Підпис, SmartID)",
  },
  BoundVia: { proof_of_possession: "доказ володіння ключем", internal_issuance: "видано внутрішнім ЦСК" },
};

export function makeLabeler(dict: Dictionaries | null) {
  const labels = (dict?.labels ?? {}) as Record<string, Record<string, string>>;
  return (enumName: string, value: string | null | undefined): string => {
    if (value === null || value === undefined) return "—";
    return labels[enumName]?.[value] ?? FALLBACK[enumName]?.[value] ?? value;
  };
}

export function toneOf(enumName: string, value: string | null | undefined): Tone {
  return (value && TONES[enumName]?.[value]) || "neutral";
}

export function fallbackLabels(enumName: string): Record<string, string> {
  return FALLBACK[enumName] ?? {};
}
