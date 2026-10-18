/**
 * Підказка щодо пароля до файлу-ключа — та сама політика, що й на сервері
 * (`sed_routing.kep.internal_ca.check_password_strength`): ≥ 12 символів, 3 види символів, без імені й пошти.
 * Остаточно перевіряє сервер; тут — щоб користувач бачив вимоги до надсилання.
 */
export function passwordProblems(password: string, fullName: string, email: string): string[] {
  const problems: string[] = [];
  if (password.length < 12) problems.push("щонайменше 12 символів");
  const classes = [/[a-zа-яіїєґ]/u, /[A-ZА-ЯІЇЄҐ]/u, /\d/u, /[^\p{L}\p{N}_\s]/u].filter((r) => r.test(password)).length;
  if (classes < 3) problems.push("щонайменше три види символів: малі, великі літери, цифри, спецсимволи");
  const low = password.toLocaleLowerCase("uk");
  const parts = `${fullName} ${email}`.toLocaleLowerCase("uk").split(/[\s@.'’-]+/u).filter((p) => p.length >= 4);
  if (parts.some((p) => low.includes(p))) problems.push("без імені, прізвища чи пошти");
  return problems;
}
