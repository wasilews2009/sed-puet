import { defineConfig } from "@playwright/test";

/** Наскрізна перевірка на стенді dev/stand.py (sed-app віддає збірку dist/). */
export default defineConfig({
  testDir: "e2e",
  timeout: 120_000,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.SED_E2E_URL ?? "http://127.0.0.1:8766",
    locale: "uk-UA",
    viewport: { width: 1440, height: 1000 },
    launchOptions: process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {},
    trace: "retain-on-failure",
  },
});
