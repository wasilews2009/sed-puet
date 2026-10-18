/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * Збірка кладеться в dist/ з базою /static/: її можна віддати самим sed-app
 * (SED_APP_WEB_ROOT=…/dist — index.html на «/», ресурси на «/static/»), без змін у бекенді і з тим самим CSP.
 * У режимі розробки Vite проксіює /api на бекенд (SED_API_URL, типово http://127.0.0.1:8080).
 */
export default defineConfig(({ command }) => ({
  base: command === "build" ? "/static/" : "/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: process.env.SED_API_URL ?? "http://127.0.0.1:8080", changeOrigin: false } },
  },
  preview: {
    port: 4173,
    proxy: { "/api": { target: process.env.SED_API_URL ?? "http://127.0.0.1:8080", changeOrigin: false } },
  },
  build: {
    target: "es2022",
    sourcemap: false,
    // жодних inline-скриптів: CSP бекенду — script-src 'self'
    modulePreload: { polyfill: false },
    chunkSizeWarningLimit: 700,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
}));
