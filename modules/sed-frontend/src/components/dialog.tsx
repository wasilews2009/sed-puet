/**
 * Модальне вікно на нативному <dialog> (фокус-пастка й Esc — браузерні) і запит причини дії
 * замість window.prompt: причина відхилення/скасування/відкликання обов'язкова і пишеться в журнал.
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Button, Field, Textarea } from "./ui";

export function Modal({ open, title, onClose, children }: { open: boolean; title: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal?.();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      aria-labelledby="dlg-title"
      onClose={onClose}
      className="w-full max-w-lg rounded-lg border border-slate-200 bg-white p-0 text-slate-900 shadow-xl backdrop:bg-slate-900/50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
    >
      {open && (
        <div className="p-5">
          <h2 id="dlg-title" className="mb-3 text-lg font-semibold">
            {title}
          </h2>
          {children}
        </div>
      )}
    </dialog>
  );
}

interface ReasonRequest {
  title: string;
  label: string;
  confirmText: string;
  minLength: number;
  danger: boolean;
  resolve: (v: string | null) => void;
}

type AskReason = (o: { title: string; label: string; confirmText?: string; minLength?: number; danger?: boolean }) => Promise<string | null>;

const Ctx = createContext<AskReason>(async () => null);

export function ReasonProvider({ children }: { children: ReactNode }) {
  const [req, setReq] = useState<ReasonRequest | null>(null);
  const [text, setText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const ask = useCallback<AskReason>(
    (o) =>
      new Promise((resolve) => {
        setText("");
        setErr(null);
        setReq({ confirmText: "Підтвердити", minLength: 3, danger: true, ...o, resolve });
      }),
    [],
  );
  const close = (v: string | null) => {
    req?.resolve(v);
    setReq(null);
  };
  return (
    <Ctx.Provider value={ask}>
      {children}
      <Modal open={!!req} title={req?.title ?? ""} onClose={() => close(null)}>
        <form
          className="flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (!req) return;
            if (text.trim().length < req.minLength) {
              setErr(`Щонайменше ${req.minLength} символи`);
              return;
            }
            close(text.trim());
          }}
        >
          <Field label={req?.label ?? ""} error={err} required>
            {(p) => <Textarea {...p} autoFocus rows={3} value={text} onChange={(e) => setText(e.target.value)} />}
          </Field>
          <div className="flex justify-end gap-2">
            <Button onClick={() => close(null)}>Скасувати</Button>
            <Button type="submit" variant={req?.danger ? "danger" : "primary"}>
              {req?.confirmText}
            </Button>
          </div>
        </form>
      </Modal>
    </Ctx.Provider>
  );
}

export function useAskReason() {
  return useContext(Ctx);
}
