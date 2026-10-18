/** Сповіщення про результат дії: озвучуються зчитувачами (aria-live), зникають самі. */
import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { cx } from "./ui";

type ToastTone = "ok" | "danger" | "info";
interface Toast {
  id: number;
  text: string;
  tone: ToastTone;
}

const Ctx = createContext<(text: string, tone?: ToastTone) => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((text: string, tone: ToastTone = "ok") => {
    const id = Date.now() + Math.random();
    setItems((xs) => [...xs, { id, text, tone }]);
    setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), tone === "danger" ? 9000 : 5000);
  }, []);
  return (
    <Ctx.Provider value={push}>
      {children}
      <div aria-live="polite" role="status" className="pointer-events-none fixed bottom-4 right-4 z-50 flex max-w-sm flex-col gap-2">
        {items.map((t) => (
          <div
            key={t.id}
            className={cx(
              "pointer-events-auto rounded-md px-4 py-3 text-sm text-white shadow-lg",
              t.tone === "ok" && "bg-emerald-700",
              t.tone === "danger" && "bg-red-700",
              t.tone === "info" && "bg-slate-800",
            )}
          >
            {t.text}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

export function useToast() {
  return useContext(Ctx);
}
