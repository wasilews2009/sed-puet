// @ts-check
/** Спільний стан сесії: користувач, довідники і підписи значень українською. */

import { pill } from "./dom.js";

/**
 * @typedef {{ role: string, role_label: string, org_unit_id: string | null, org_unit_name: string | null, scope_note: string | null, is_acting: boolean }} Role
 * @typedef {{ id: string, display_name: string, email: string, primary_org_unit_id: string | null, roles: Role[],
 *   permissions: { upload_all_units: boolean, upload_unit_ids: string[], corpus_admin: boolean, view_all_documents: boolean } }} Me
 * @typedef {{ id: string, parent_id: string | null, kind: string, code: string, name: string, short_name: string | null }} OrgUnit
 * @typedef {{ org_units: OrgUnit[], document_types: { id: string, code: string, name: string, requires_kep: boolean }[],
 *   labels: Record<string, Record<string, string>>, field_labels: Record<string, string>, confidence_threshold: string,
 *   modules: { ingest: boolean, compliance: boolean, routing: boolean, kep_mock: boolean } }} Dictionaries
 */

export const state = {
  /** @type {Me | null} */ me: null,
  /** @type {Dictionaries | null} */ dict: null,
};

export function threshold() {
  return Number(state.dict?.confidence_threshold ?? 0.85);
}

/** @param {string} enumName @param {string | null | undefined} value */
export function L(enumName, value) {
  if (value === null || value === undefined) return "—";
  return state.dict?.labels?.[enumName]?.[value] ?? value;
}

/** @type {Record<string, Record<string, "" | "ok" | "warn" | "danger" | "accent">>} */
const TONES = {
  DocumentStatus: {
    ai_review: "accent", on_approval: "accent", revision_required: "warn", approved: "ok", signed: "ok",
    registered: "ok", in_execution: "accent", executed: "ok", cancelled: "danger",
  },
  StepStatus: { active: "accent", approved: "ok", approved_with_remarks: "warn", rejected: "danger", expired: "danger", skipped: "" },
  RouteStatus: { active: "accent", completed: "ok", rejected: "danger", cancelled: "danger" },
  HumanReviewStatus: { pending: "warn", confirmed: "ok", corrected: "ok", rejected: "danger" },
  ComplianceVerdict: { compliant: "ok", issues_found: "warn", non_compliant: "danger", inconclusive: "warn" },
  FindingSeverity: { critical: "danger", major: "warn", minor: "accent" },
  FindingStatus: { open: "warn", accepted: "accent", dismissed: "", fixed: "ok" },
  SignatureValidation: { valid: "ok", invalid: "danger", cert_revoked: "danger", cert_expired: "danger", indeterminate: "warn" },
  AccessLevel: { restricted: "warn", personal_data: "danger", public: "ok" },
};

/** @param {string} enumName @param {string | null | undefined} value */
export function status(enumName, value) {
  return pill(L(enumName, value), (value && TONES[enumName]?.[value]) || "");
}

/** @param {string | null | undefined} id */
export function unitName(id) {
  if (!id) return "—";
  const u = state.dict?.org_units.find((x) => x.id === id);
  return u ? u.name : "—";
}
