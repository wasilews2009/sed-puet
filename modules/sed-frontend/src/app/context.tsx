/** Контекст сесії: конфігурація входу, поточний користувач, довідники, підписи значень. */
import { createContext, useContext, useMemo, type ReactNode } from "react";
import type { AuthConfig, Dictionaries, Me } from "../api/types";
import { makeLabeler } from "../domain/labels";

export interface AppState {
  config: AuthConfig;
  me: Me;
  dict: Dictionaries;
  L: (enumName: string, value: string | null | undefined) => string;
  threshold: number;
  unitName: (id: string | null | undefined) => string;
  fieldLabel: (key: string) => string;
  logout: () => void;
}

const Ctx = createContext<AppState | null>(null);

export function AppProvider({
  config,
  me,
  dict,
  logout,
  children,
}: {
  config: AuthConfig;
  me: Me;
  dict: Dictionaries;
  logout: () => void;
  children: ReactNode;
}) {
  const value = useMemo<AppState>(() => {
    const L = makeLabeler(dict);
    const fieldLabels = (dict.field_labels ?? {}) as Record<string, string>;
    return {
      config,
      me,
      dict,
      L,
      threshold: Number(dict.confidence_threshold ?? 0.85),
      unitName: (id) => dict.org_units.find((u) => u.id === id)?.name ?? "—",
      fieldLabel: (k) => fieldLabels[k] ?? k,
      logout,
    };
  }, [config, me, dict, logout]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): AppState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useApp поза AppProvider");
  return v;
}
