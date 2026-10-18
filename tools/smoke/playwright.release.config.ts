import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "e2e-release", timeout: 600_000, workers: 1, reporter: [["list"]],
  use: { baseURL: "http://localhost:8080", locale: "uk-UA", viewport: { width: 1440, height: 1000 },
         launchOptions: { executablePath: process.env.PW_CHROMIUM }, trace: "retain-on-failure" },
});
