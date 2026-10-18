/**
 * Смоук-тест релізу: docker compose стек (nginx → sed-app), вхід через Keycloak (OIDC + PKCE),
 * реєстрація → реквізити ШІ (HITL) → нормативний аудит → маршрут з 5 візами КЕП (імітаційний КНЕДП) → «Погоджено».
 */
import { expect, test, type Page } from "@playwright/test";
import { mkdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

const FIX = process.env.SED_FIXTURES!;
const SHOTS = process.env.SED_SHOTS!;
mkdirSync(SHOTS, { recursive: true });
const NEW_PASSWORD = "Sed-Smoke-2026!x";
const changed = new Set<string>();
let shot = 0;
const snap = (page: Page, name: string) => page.screenshot({ path: `${SHOTS}/${String(++shot).padStart(2, "0")}-${name}.png`, fullPage: true });

async function login(page: Page, username: string) {
  await page.goto("/");
  const logout = page.getByRole("button", { name: "Вийти" });
  const sso = page.getByRole("button", { name: /Увійти через SSO/ });
  await expect(logout.or(sso).first()).toBeVisible({ timeout: 30_000 });
  if (await logout.isVisible()) {
    await logout.click();
    const kcLogout = page.locator("#kc-logout");
    await expect(kcLogout.or(sso).first()).toBeVisible({ timeout: 30_000 });
    if (await kcLogout.isVisible()) await kcLogout.click();
    await page.goto("/");
  }
  await sso.click();
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(changed.has(username) ? NEW_PASSWORD : "demo");
  if (shot === 1) await snap(page, "keycloak-login");
  await page.locator("#kc-login").click();
  const pwNew = page.locator("#password-new");
  await expect(pwNew.or(page.getByRole("button", { name: "Вийти" })).first()).toBeVisible({ timeout: 30_000 });
  if (await pwNew.isVisible()) {
    console.log(`${username}: Keycloak вимагає змінити тимчасовий пароль`);
    await pwNew.fill(NEW_PASSWORD);
    await page.locator("#password-confirm").fill(NEW_PASSWORD);
    await page.locator("input[type=submit], button[type=submit]").first().click();
    changed.add(username);
  }
  await expect(page.getByRole("button", { name: "Вийти" })).toBeVisible({ timeout: 30_000 });
}

test("реліз: від реєстрації до «Погоджено» через Keycloak", async ({ page }) => {
  test.setTimeout(600_000);
  const consoleErrors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });

  await page.goto("/");
  await expect(page.getByRole("button", { name: /Увійти через SSO/ })).toBeVisible();
  await snap(page, "login");
  await login(page, "lecturer");
  await snap(page, "dashboard");

  await page.getByRole("link", { name: "Зареєструвати" }).first().click();
  const pdf = Buffer.concat([readFileSync(`${FIX}/memo-1.pdf`), Buffer.from(`\n% run ${Date.now()}\n`)]);
  await page.getByLabel(/Файл документа/).setInputFiles({ name: "zapyska.pdf", mimeType: "application/pdf", buffer: pdf });
  await page.getByRole("button", { name: "Зареєструвати і розпізнати" }).click();
  await expect(page.getByText(/Документ зареєстровано/)).toBeVisible({ timeout: 120_000 });
  await snap(page, "registered");
  await page.getByRole("link", { name: "Відкрити картку" }).click();

  const docId = /documents\/([0-9a-f-]{36})/.exec(page.url())![1]!;
  const edit = page.getByRole("button", { name: "Виправити" });
  await expect(edit.first()).toBeVisible();
  await snap(page, "requisites-ai");
  if (await page.getByRole("button", { name: "Підтвердити" }).count()) {
    await page.getByRole("button", { name: "Підтвердити" }).first().click();
    await page.waitForTimeout(800);
  }
  const accept = page.getByRole("button", { name: /Перенести в картку впевнені значення/ });
  if (await accept.count()) {
    await accept.click();
    await expect(page.getByText(/Перенесено в картку/)).toBeVisible();
  }
  await snap(page, "requisites-accepted");

  await page.getByRole("tab", { name: /Нормативний аудит/ }).click();
  await page.getByRole("button", { name: /Перевірити на відповідність нормам/ }).click();
  await expect(page.getByRole("button", { name: /Перевірити повторно/ })).toBeVisible({ timeout: 180_000 });
  await snap(page, "compliance");

  await page.getByRole("tab", { name: "Погодження" }).click();
  await page.getByRole("button", { name: "Сформувати маршрут" }).click();
  await expect(page.getByRole("button", { name: "Запустити погодження" })).toBeVisible();
  await snap(page, "route-preview");
  await page.getByRole("button", { name: "Запустити погодження" }).click();
  const confirm = page.getByRole("dialog").getByRole("button");
  await page.waitForTimeout(500);
  if (await page.getByRole("dialog").count()) await confirm.last().click();
  await expect(page.getByText(/Маршрут запущено/)).toBeVisible();

  for (const who of ["head.kknit", "vicerector", "buh", "rector", "pfv"]) {
    await login(page, who);
    await page.getByRole("navigation", { name: "Розділи" }).getByRole("link", { name: /На погодження/ }).click();
    await page.locator(`a[href="#/documents/${docId}/route"]`).click();
    await expect(page.getByRole("heading", { name: /Ваше рішення/ })).toBeVisible();
    await page.getByRole("radio", { name: /Тестовий ключ стенду/ }).check();
    if (who === "head.kknit") await snap(page, "decision");
    await page.getByRole("button", { name: /Погодити і підписати/ }).click();
    await expect(page.getByText(/Візу з КЕП накладено/)).toBeVisible({ timeout: 30_000 });
  }

  await page.goto(`/#/documents/${docId}/versions`);
  await expect(page.getByText("Погоджено").first()).toBeVisible();
  await expect(page.getByRole("table", { name: "Підписи" }).getByText("Чинний")).toHaveCount(5);
  await snap(page, "approved");

  await login(page, "student");
  await page.goto(`/#/documents/${docId}`);
  await expect(page.getByText("Документ не знайдено")).toBeVisible();
  console.log("console errors:", JSON.stringify(consoleErrors));
  expect(consoleErrors.filter((e) => !/404|Failed to load resource/.test(e))).toEqual([]);
});
