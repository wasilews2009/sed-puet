/**
 * Навігація залежно від ролей і прав. Це лише зручність інтерфейсу: справжню перевірку
 * доступу робить бекенд на кожному запиті (недоступний документ повертає 404).
 */
import type { Dictionaries, Me, UserRole } from "../api/types";

export type NavKey = "dashboard" | "documents" | "upload" | "inbox" | "review" | "regulations" | "draft-check" | "keys";

export interface NavItem {
  key: NavKey;
  to: string;
  label: string;
  badge?: "inbox" | "review";
}

const STAFF_ROLES: ReadonlySet<UserRole> = new Set([
  "lecturer",
  "department_head",
  "institute_director",
  "unit_head",
  "rector",
  "vice_rector",
  "staff",
  "student_government_head",
]);

export function roleSet(me: Me): Set<UserRole> {
  return new Set(me.roles.map((r) => r.role));
}

export function canUpload(me: Me): boolean {
  return me.permissions.upload_all_units || me.permissions.upload_unit_ids.length > 0;
}

/** Хто має працювати з ключами підпису: усі, хто може бути учасником маршруту (не лише студенти). */
export function canHaveSigningKeys(me: Me): boolean {
  return [...roleSet(me)].some((r) => STAFF_ROLES.has(r));
}

export function navigation(me: Me, dict: Dictionaries): NavItem[] {
  const items: NavItem[] = [
    { key: "dashboard", to: "/", label: "Огляд" },
    { key: "documents", to: "/documents", label: "Документи" },
  ];
  if (dict.modules.ingest && canUpload(me)) items.push({ key: "upload", to: "/upload", label: "Зареєструвати" });
  if (dict.modules.routing) items.push({ key: "inbox", to: "/inbox", label: "На погодження", badge: "inbox" });
  if (me.roles.length) items.push({ key: "review", to: "/review", label: "Перевірка ШІ", badge: "review" });
  if (dict.modules.compliance) {
    items.push({ key: "regulations", to: "/regulations", label: "Нормативна база" });
    if (canUpload(me)) items.push({ key: "draft-check", to: "/draft-check", label: "Перевірка проєкту" });
  }
  if (dict.modules.routing && canHaveSigningKeys(me)) items.push({ key: "keys", to: "/keys", label: "Ключі підпису" });
  return items;
}
