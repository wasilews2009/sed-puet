/** Вхід: Keycloak SSO університету або (лише стенд розробника) вибір демо-користувача. */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { authApi } from "../api/endpoints";
import type { AuthConfig } from "../api/types";
import { startLogin } from "../auth/oidc";
import { saveSession } from "../auth/session";
import { Button, Card, ErrorBox, Loading, Notice } from "../components/ui";

export function LoginPage({ config, reason, onLoggedIn }: { config: AuthConfig; reason?: string; onLoggedIn: () => void }) {
  return (
    <main id="main" className="mx-auto mt-12 max-w-3xl space-y-4 p-4">
      <div className="flex items-center gap-3">
        <span aria-hidden="true" className="grid h-12 w-12 place-items-center rounded-lg bg-brand-700 text-lg font-bold text-white">
          СЕД
        </span>
        <div>
          <h1 className="text-2xl font-semibold">СЕД ПУЕТ</h1>
          <p className="text-sm text-slate-600 dark:text-slate-400">
            Система електронного документообігу Полтавського університету економіки і торгівлі з ШІ-асистентом
          </p>
        </div>
      </div>
      {reason && <Notice tone="warn">{reason}</Notice>}
      <Card>{config.mode === "keycloak" ? <SsoLogin config={config} /> : <DevLogin onLoggedIn={onLoggedIn} />}</Card>
      <p className="text-xs text-slate-600 dark:text-slate-400">
        ШІ є асистентом: його результати перевіряє людина. Юридичної сили документ набуває лише після накладання КЕП посадовою особою.
      </p>
    </main>
  );
}

function SsoLogin({ config }: { config: AuthConfig }) {
  const [err, setErr] = useState<unknown>(null);
  return (
    <div className="space-y-3">
      <p>Вхід через єдиний обліковий запис університету (Keycloak SSO).</p>
      <Button variant="primary" onClick={() => startLogin(config).catch(setErr)}>
        Увійти через SSO ПУЕТ
      </Button>
      {err != null && <ErrorBox error={err} />}
    </div>
  );
}

function DevLogin({ onLoggedIn }: { onLoggedIn: () => void }) {
  const users = useQuery({ queryKey: ["dev-users"], queryFn: authApi.devUsers });
  const [err, setErr] = useState<unknown>(null);
  return (
    <div className="space-y-3">
      <Notice tone="warn">Стенд розробника: вхід демо-користувачем без пароля. У production цей режим вимкнено.</Notice>
      {users.isLoading && <Loading />}
      {users.error && <ErrorBox error={users.error} />}
      {err != null && <ErrorBox error={err} />}
      <ul className="grid gap-2 sm:grid-cols-2" aria-label="Демо-користувачі">
        {(users.data ?? []).map((u) => (
          <li key={u.id}>
            <button
              type="button"
              className="w-full rounded-md border border-slate-200 p-3 text-left hover:border-brand-600 hover:bg-brand-50 focus-visible:outline-brand-600 dark:border-slate-700 dark:hover:bg-slate-800"
              onClick={async () => {
                try {
                  const t = await authApi.devLogin(u.id);
                  saveSession(t.access_token, t.expires_in);
                  onLoggedIn();
                } catch (e) {
                  setErr(e);
                }
              }}
            >
              <span className="block font-semibold">{u.display_name}</span>
              <span className="block text-xs text-slate-600 dark:text-slate-400">{u.roles.join(", ")}</span>
              <span className="block text-xs text-slate-500">{u.email}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
