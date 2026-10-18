/**
 * Завантаження застосунку: конфігурація входу → повернення з Keycloak → сесія → профіль і довідники.
 * Маршрутизація на #hash: бекенд віддає index.html лише на «/», а Keycloak повертає на корінь.
 */
import { useCallback, useEffect, useState } from "react";
import { HashRouter, Navigate, Route, Routes } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { onUnauthorized } from "../api/client";
import { authApi } from "../api/endpoints";
import type { AuthConfig, Dictionaries, Me } from "../api/types";
import { completeLogin, logoutUrl } from "../auth/oidc";
import { readSession } from "../auth/session";
import { ReasonProvider } from "../components/dialog";
import { ToastProvider } from "../components/toast";
import { Button, ErrorBox, Loading } from "../components/ui";
import { DashboardPage } from "../features/dashboard/DashboardPage";
import { RegistryPage } from "../features/documents/RegistryPage";
import { UploadPage } from "../features/documents/UploadPage";
import { CardPage } from "../features/card/CardPage";
import { InboxPage } from "../features/queues/InboxPage";
import { ReviewQueuePage } from "../features/queues/ReviewQueuePage";
import { RegulationsPage } from "../features/regulations/RegulationsPage";
import { DraftCheckPage } from "../features/regulations/DraftCheckPage";
import { KeysPage } from "../features/keys/KeysPage";
import { AppProvider } from "./context";
import { Layout } from "./Layout";
import { LoginPage } from "./LoginPage";

type Boot =
  | { stage: "loading" }
  | { stage: "error"; error: unknown; config?: AuthConfig }
  | { stage: "login"; config: AuthConfig; reason?: string }
  | { stage: "ready"; config: AuthConfig; me: Me; dict: Dictionaries };

export function App() {
  const [boot, setBoot] = useState<Boot>({ stage: "loading" });
  const qc = useQueryClient();

  const start = useCallback(async () => {
    let config: AuthConfig | undefined;
    try {
      config = await authApi.config();
      await completeLogin(config);
      if (!readSession()) {
        setBoot({ stage: "login", config });
        return;
      }
      const [me, dict] = await Promise.all([authApi.me(), authApi.dictionaries()]);
      setBoot({ stage: "ready", config, me, dict });
    } catch (error) {
      if (config && !readSession()) setBoot({ stage: "login", config });
      else setBoot({ stage: "error", error, config });
    }
  }, []);

  useEffect(() => {
    void start();
  }, [start]);

  useEffect(
    () =>
      onUnauthorized(() => {
        qc.clear();
        setBoot((b) => (b.stage === "ready" ? { stage: "login", config: b.config, reason: "Сесія завершилась. Увійдіть знову." } : b));
      }),
    [qc],
  );

  const logout = useCallback(() => {
    if (boot.stage !== "ready") return;
    qc.clear();
    const url = logoutUrl(boot.config);
    if (url) location.assign(url);
    else setBoot({ stage: "login", config: boot.config });
  }, [boot, qc]);

  if (boot.stage === "loading") return <Loading label="Завантаження СЕД ПУЕТ…" />;
  if (boot.stage === "error")
    return (
      <main className="mx-auto mt-16 max-w-lg space-y-4 p-4">
        <ErrorBox error={boot.error} />
        <Button onClick={() => void start()}>Спробувати ще раз</Button>
      </main>
    );
  if (boot.stage === "login")
    return (
      <ToastProvider>
        <LoginPage config={boot.config} reason={boot.reason} onLoggedIn={() => void start()} />
      </ToastProvider>
    );

  return (
    <AppProvider config={boot.config} me={boot.me} dict={boot.dict} logout={logout}>
      <ToastProvider>
        <ReasonProvider>
          <HashRouter>
            <Routes>
              <Route element={<Layout />}>
                <Route index element={<DashboardPage />} />
                <Route path="documents" element={<RegistryPage />} />
                <Route path="documents/:id" element={<CardPage />} />
                <Route path="documents/:id/:tab" element={<CardPage />} />
                <Route path="upload" element={<UploadPage />} />
                <Route path="inbox" element={<InboxPage />} />
                <Route path="review" element={<ReviewQueuePage />} />
                <Route path="regulations" element={<RegulationsPage />} />
                <Route path="draft-check" element={<DraftCheckPage />} />
                <Route path="keys" element={<KeysPage />} />
                <Route path="keys/:section" element={<KeysPage />} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Route>
            </Routes>
          </HashRouter>
        </ReasonProvider>
      </ToastProvider>
    </AppProvider>
  );
}
