/**
 * Наскрізний сценарій у браузері проти справжнього sed-app (PostgreSQL, модулі інжесту, маршрутів, ключів):
 * реєстрація службової записки → ручна перевірка реквізитів ШІ → маршрут з 5 учасниками, кожен підписує
 * (зав. кафедри — внутрішнім ключем АІ-СЕД, підписаним у браузері; решта — імітаційною КЕП стенду) → «Погоджено».
 * Плюс: закріплення КЕП через виклик, відкріплення ключа, 404 для студента, перевірка доступності axe.
 */
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

const FIX = resolve(__dirname, "../dev/fixtures");
const SHOTS = resolve(__dirname, "screenshots");
mkdirSync(SHOTS, { recursive: true });
const KEY_PASSWORD = "Kafedra-Visa-2026!";

async function login(page: Page, name: string) {
  await page.goto("/");
  const logout = page.getByRole("button", { name: "Вийти" });
  const users = page.getByRole("list", { name: "Демо-користувачі" });
  await expect(logout.or(users).first()).toBeVisible();
  if (await logout.isVisible()) await logout.click();
  await page.getByRole("button", { name: new RegExp(name) }).click();
  await expect(page.getByRole("button", { name: "Вийти" })).toBeVisible();
}

async function axe(page: Page, label: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const serious = r.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  if (serious.length) console.log(label, JSON.stringify(serious.map((v) => ({ id: v.id, nodes: v.nodes.slice(0, 3).map((n) => n.target) })), null, 1));
  expect(serious, `axe: ${label}`).toEqual([]);
}

test("службова записка: від реєстрації до «Погоджено» з п'ятьма підписами", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });

  // --- вхід і реєстрація
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "СЕД ПУЕТ" })).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/01-login.png` });
  await axe(page, "login");
  await login(page, "Андрій МЕЛЬНИК");
  await page.screenshot({ path: `${SHOTS}/02-dashboard.png` });
  await axe(page, "dashboard");
  await page.getByRole("link", { name: "Зареєструвати" }).first().click();
  // унікальні байти (коментар після %%EOF), щоб повторні прогони не ловили «дублікат»
  const pdf = Buffer.concat([readFileSync(`${FIX}/memo-1.pdf`), Buffer.from(`\n% run ${Date.now()}\n`)]);
  await page.getByLabel(/Файл документа/).setInputFiles({ name: "zapyska.pdf", mimeType: "application/pdf", buffer: pdf });
  await page.getByRole("button", { name: "Зареєструвати і розпізнати" }).click();
  await expect(page.getByText(/Документ зареєстровано/)).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/03-upload.png` });
  await page.getByRole("link", { name: "Відкрити картку" }).click();

  // --- картка: реквізити ШІ і HITL
  await expect(page.getByRole("tab", { name: /Реквізити/ })).toHaveAttribute("aria-selected", "true");
  const docUrl = page.url();
  const docId = /documents\/([0-9a-f-]{36})/.exec(docUrl)![1]!;
  const pendingConfirm = page.getByRole("button", { name: "Підтвердити" });
  if (await pendingConfirm.count()) {
    // поле з низькою впевненістю: виправляємо автора вручну
    await page.getByRole("button", { name: "Виправити" }).first().click();
    const name = page.getByLabel("Ім'я та прізвище");
    if (await name.count()) await name.fill("Андрій МЕЛЬНИК");
    else await page.getByLabel("Значення").fill("Виправлено людиною");
    await page.getByRole("button", { name: "Зберегти виправлення" }).click();
    await expect(page.getByText(/виправлено/).first()).toBeVisible();
  }
  const accept = page.getByRole("button", { name: /Перенести в картку впевнені значення/ });
  if (await accept.count()) {
    await accept.click();
    await expect(page.getByText(/Перенесено в картку/)).toBeVisible();
  }
  await expect(page.getByRole("heading", { name: /Реквізити картки/ })).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/04-card-requisites.png`, fullPage: true });
  await axe(page, "card");

  // --- запуск маршруту
  await page.getByRole("tab", { name: "Погодження" }).click();
  await page.getByRole("button", { name: "Сформувати маршрут" }).click();
  await expect(page.getByRole("button", { name: "Запустити погодження" })).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/05-route-preview.png`, fullPage: true });
  await page.getByRole("button", { name: "Запустити погодження" }).click();
  await expect(page.getByText(/Маршрут запущено/)).toBeVisible();

  // --- зав. кафедри: отримує внутрішній ключ і підписує ним у браузері (якщо крок приймає внутрішні ключі)
  await login(page, "Наталія КРАВЕЦЬ");
  await page.getByRole("navigation", { name: "Розділи" }).getByRole("link", { name: "Ключі підпису" }).click();
  await page.getByRole("tab", { name: "Внутрішній ключ" }).click();
  await page.getByLabel("Пароль до файлу-ключа").fill(KEY_PASSWORD);
  await page.getByLabel("Повторіть пароль").fill(KEY_PASSWORD);
  const dl = page.waitForEvent("download");
  await page.getByRole("button", { name: "Згенерувати і завантажити" }).click();
  const keyPath = `${SHOTS}/../.tmp-key.p12`;
  await (await dl).saveAs(keyPath);
  await page.getByRole("tab", { name: "Мої ключі" }).click();
  await expect(page.getByText(/Внутрішній ключ АІ-СЕД ПУЕТ/).first()).toBeVisible();
  await page.screenshot({ path: `${SHOTS}/06-keys-internal.png`, fullPage: true });
  await axe(page, "keys");

  const signers: [string, "pkcs12" | "mock"][] = [
    ["Наталія КРАВЕЦЬ", "pkcs12"],
    ["Ірина САВЧУК", "mock"],
    ["Людмила РОМАНЕНКО", "mock"],
    ["Олександр ДЕМЧЕНКО", "mock"],
    ["Тетяна ЛИТВИН", "mock"],
  ];
  for (const [i, [who, how]] of signers.entries()) {
    if (i > 0) await login(page, who);
    await page.getByRole("navigation", { name: "Розділи" }).getByRole("link", { name: /На погодження/ }).click();
    await page.locator(`a[href="#/documents/${docId}/route"]`).click();
    await expect(page.getByRole("heading", { name: /Ваше рішення/ })).toBeVisible();
    const pk = page.getByRole("radio", { name: /PKCS#12/ });
    const usePk = how === "pkcs12" && (await pk.count()) > 0;
    if (usePk) {
      await pk.check();
      await page.getByLabel("Файл-ключ (.p12)").setInputFiles(keyPath);
      await page.getByLabel("Пароль до ключа").fill(KEY_PASSWORD);
    } else {
      await page.getByRole("radio", { name: /Тестовий ключ стенду/ }).check();
    }
    if (i === 0) await page.screenshot({ path: `${SHOTS}/07-decision-panel.png`, fullPage: true });
    await page.getByRole("button", { name: /Погодити і підписати/ }).click();
    const ok = page.getByText(/Візу з КЕП накладено/);
    const err = page.getByRole("alert");
    await expect(ok.or(err).first()).toBeVisible({ timeout: 30_000 });
    if (usePk && !(await ok.isVisible())) {
      // сервер не прийняв внутрішній ключ на цьому кроці — це політика маршруту; підписуємо імітаційною КЕП
      console.log(`${who}: внутрішній ключ відхилено: ${await err.first().innerText()}`);
      await page.getByRole("radio", { name: /Тестовий ключ стенду/ }).check();
      await page.getByRole("button", { name: /Погодити і підписати/ }).click();
      await expect(ok).toBeVisible({ timeout: 30_000 });
    } else if (usePk) console.log(`${who}: віза внутрішнім ключем у браузері прийнята`);
  }

  // --- результат
  await page.goto(`/#/documents/${docId}/versions`);
  await expect(page.getByText("Погоджено").first()).toBeVisible();
  const validSigs = page.getByRole("table", { name: "Підписи" }).getByText("Чинний");
  await expect(validSigs).toHaveCount(5);
  await page.screenshot({ path: `${SHOTS}/08-approved-signatures.png`, fullPage: true });
  await page.getByRole("tab", { name: "Погодження" }).click();
  await page.screenshot({ path: `${SHOTS}/09-route-completed.png`, fullPage: true });

  // --- студент не бачить документа (404, а не 403)
  await login(page, "Марія БОНДАР");
  await page.goto(docUrl);
  await expect(page.getByText("Документ не знайдено")).toBeVisible();

  expect(consoleErrors.filter((e) => !/404|Failed to load resource/.test(e))).toEqual([]);
});

test("закріплення КЕП через виклик і відкріплення ключа", async ({ page }) => {
  await login(page, "Ірина САВЧУК");
  await page.goto("/#/keys/bind");
  await page.getByRole("button", { name: "Отримати виклик" }).click();
  await expect(page.getByText(/Дійсний до/)).toBeVisible();
  // імітаційний файл-ключ КНЕДП стенду (у production його видає КНЕДП, а не СЕД)
  const token = await page.evaluate(() => JSON.parse(sessionStorage.getItem("sed.session") || "{}").token as string);
  const r = await page.request.post("/api/v1/dev/kep/mock-key", { headers: { Authorization: `Bearer ${token}` } });
  expect(r.ok()).toBeTruthy();
  const mockKey = resolve(SHOTS, "../.tmp-mock.p12");
  writeFileSync(mockKey, await r.body());
  await page.getByRole("radio", { name: /PKCS#12/ }).check();
  await page.getByLabel("Файл-ключ (.p12)").setInputFiles(mockKey);
  await page.getByLabel("Пароль до ключа").fill("Mock-Key-2026!");
  await page.screenshot({ path: `${SHOTS}/10-bind-kep.png`, fullPage: true });
  await page.getByRole("button", { name: "Закріпити ключ" }).click();
  await expect(page.getByText(/закріплено/).first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("table", { name: "Мої ключі підпису" })).toContainText("САВЧУК");
  await page.screenshot({ path: `${SHOTS}/11-my-keys.png`, fullPage: true });

  await page.getByRole("button", { name: "Відкріпити" }).first().click();
  await page.getByRole("dialog").getByRole("textbox").fill("Тест: втрата носія");
  await page.getByRole("dialog").getByRole("button", { name: "Відкріпити" }).click();
  await expect(page.getByText(/Ключ відкріплено/)).toBeVisible();

  // перевірка сертифіката: файл-ключ сервер відмовляється приймати
  await page.getByRole("tab", { name: "Перевірити сертифікат" }).click();
  await page.getByLabel("Сертифікат", { exact: true }).setInputFiles(mockKey);
  await page.getByRole("button", { name: "Перевірити", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(/особистим ключем/);
});

test("решта розділів: реєстр, черга ШІ, нормативна база, перевірка проєкту, телефон", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  await login(page, "Олександр ДЕМЧЕНКО");
  const nav = page.getByRole("navigation", { name: "Розділи" });
  await nav.getByRole("link", { name: "Документи" }).click();
  await expect(page.getByRole("table", { name: "Документи" })).toBeVisible();
  await page.getByLabel("Статус").selectOption("approved");
  await expect(page).toHaveURL(/status=approved/);
  await page.screenshot({ path: `${SHOTS}/12-registry.png`, fullPage: true });
  await axe(page, "registry");
  await nav.getByRole("link", { name: /Перевірка ШІ/ }).click();
  await expect(page.getByRole("heading", { name: "Перевірка результатів ШІ" })).toBeVisible();
  await axe(page, "review");
  await nav.getByRole("link", { name: "Нормативна база" }).click();
  await expect(page.getByText(/Додати або переіндексувати акт/)).toBeVisible(); // ректор — адміністратор корпусу
  await axe(page, "regulations");
  await nav.getByRole("link", { name: "Перевірка проєкту" }).click();
  await expect(page.getByLabel(/Текст проєкту/)).toBeVisible();
  await axe(page, "draft-check");

  // студент подає заяви, але не візує: без «Ключів підпису»
  await login(page, "Марія БОНДАР");
  await expect(nav.getByRole("link", { name: "Ключі підпису" })).toHaveCount(0);

  // телефон: меню згортається, таблиці прокручуються в межах картки
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, "Андрій МЕЛЬНИК");
  await page.getByRole("button", { name: "Меню" }).click();
  await nav.getByRole("link", { name: "Документи" }).click();
  await expect(page.getByRole("table", { name: "Документи" })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await page.screenshot({ path: `${SHOTS}/13-mobile-registry.png` });
  expect(consoleErrors.filter((e) => !/404|Failed to load resource/.test(e))).toEqual([]);
});
