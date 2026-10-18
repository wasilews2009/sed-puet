/** Базові елементи інтерфейсу: кнопки, позначки, повідомлення, поля. Доступність — WCAG 2.1 AA. */
import {
  forwardRef,
  useId,
  useState,
  type ButtonHTMLAttributes,
  type ReactNode,
  type InputHTMLAttributes,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import type { Tone } from "../domain/labels";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

type Variant = "primary" | "secondary" | "ok" | "danger" | "ghost";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-brand-700 text-white hover:bg-brand-800 border-brand-700",
  secondary:
    "bg-white text-slate-800 hover:bg-slate-100 border-slate-300 dark:bg-slate-800 dark:text-slate-100 dark:border-slate-600 dark:hover:bg-slate-700",
  ok: "bg-emerald-700 text-white hover:bg-emerald-800 border-emerald-700",
  danger: "bg-white text-red-700 hover:bg-red-50 border-red-300 dark:bg-slate-800 dark:text-red-300 dark:border-red-800",
  ghost: "bg-transparent border-transparent text-brand-700 hover:underline dark:text-brand-100",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: "sm" | "md";
  busy?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", busy, className, children, disabled, type = "button", ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      className={cx(
        "inline-flex items-center justify-center gap-2 rounded-md border font-medium transition-colors",
        "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600",
        "disabled:cursor-not-allowed disabled:opacity-60",
        size === "sm" ? "px-2.5 py-1 text-sm" : "px-4 py-2 text-sm",
        VARIANTS[variant],
        className,
      )}
      {...rest}
    >
      {busy && <Spinner small />}
      {children}
    </button>
  );
});

/** Кнопка асинхронної дії: блокується на час запиту, помилку показує поруч (role=alert). */
export function ActionButton({
  onAction,
  children,
  ...rest
}: Omit<ButtonProps, "onClick"> & { onAction: () => Promise<unknown> | unknown }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <span className="inline-flex flex-col gap-1">
      <Button
        {...rest}
        busy={busy}
        onClick={async () => {
          setBusy(true);
          setError(null);
          try {
            await onAction();
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        {children}
      </Button>
      {error && (
        <span role="alert" className="max-w-md text-sm text-red-700 dark:text-red-300">
          {error}
        </span>
      )}
    </span>
  );
}

export function Spinner({ small, label }: { small?: boolean; label?: string }) {
  return (
    <span role={label ? "status" : undefined} className="inline-flex items-center gap-2">
      <span
        aria-hidden="true"
        className={cx(
          "inline-block animate-spin rounded-full border-2 border-current border-r-transparent",
          small ? "h-3.5 w-3.5" : "h-5 w-5",
        )}
      />
      {label && <span>{label}</span>}
    </span>
  );
}

const PILL: Record<Tone, string> = {
  neutral: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-200",
  ok: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/50 dark:text-emerald-200",
  warn: "bg-amber-100 text-amber-900 dark:bg-amber-900/50 dark:text-amber-200",
  danger: "bg-red-100 text-red-800 dark:bg-red-900/50 dark:text-red-200",
  accent: "bg-sky-100 text-sky-800 dark:bg-sky-900/50 dark:text-sky-200",
};

export function Pill({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span title={title} className={cx("inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium", PILL[tone])}>
      {children}
    </span>
  );
}

const NOTICE: Record<Tone, string> = {
  neutral: "border-slate-300 bg-slate-50 dark:border-slate-700 dark:bg-slate-900",
  ok: "border-emerald-300 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950",
  warn: "border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950",
  danger: "border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950",
  accent: "border-sky-300 bg-sky-50 dark:border-sky-800 dark:bg-sky-950",
};

export function Notice({ tone = "neutral", title, children }: { tone?: Tone; title?: ReactNode; children?: ReactNode }) {
  return (
    <div role={tone === "danger" ? "alert" : undefined} className={cx("rounded-md border-l-4 px-4 py-3 text-sm", NOTICE[tone])}>
      {title && <div className="font-semibold">{title}</div>}
      {children}
    </div>
  );
}

export function Card({ children, className, as: As = "section", ...rest }: { children: ReactNode; className?: string; as?: "section" | "div" | "article"; "aria-labelledby"?: string }) {
  return (
    <As
      className={cx("rounded-lg border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900", className)}
      {...rest}
    >
      {children}
    </As>
  );
}

export function PageHeader({ title, lead, actions }: { title: string; lead?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {lead && <p className="mt-1 max-w-3xl text-sm text-slate-600 dark:text-slate-400">{lead}</p>}
      </div>
      {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-lg border border-dashed border-slate-300 p-8 text-center text-sm text-slate-600 dark:border-slate-700 dark:text-slate-400">{children}</div>;
}

export function Loading({ label = "Завантаження…" }: { label?: string }) {
  return (
    <div className="p-8 text-center text-slate-600 dark:text-slate-400">
      <Spinner label={label} />
    </div>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  return <Notice tone="danger">{error instanceof Error ? error.message : String(error)}</Notice>;
}

const inputCls =
  "w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-brand-600 focus:outline-none focus:ring-2 focus:ring-brand-600/30 dark:border-slate-600 dark:bg-slate-800 aria-[invalid=true]:border-red-500";

/** Поле з підписом, підказкою і помилкою, зв'язаними через aria-describedby. */
export function Field({
  label,
  hint,
  error,
  children,
  required,
}: {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  required?: boolean;
  children: (props: { id: string; "aria-describedby"?: string; "aria-invalid"?: boolean; required?: boolean }) => ReactNode;
}) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errId = error ? `${id}-err` : undefined;
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
        {required && <span aria-hidden="true" className="text-red-700"> *</span>}
      </label>
      {children({ id, "aria-describedby": [hintId, errId].filter(Boolean).join(" ") || undefined, "aria-invalid": !!error || undefined, required })}
      {hint && (
        <span id={hintId} className="text-xs text-slate-600 dark:text-slate-400">
          {hint}
        </span>
      )}
      {error && (
        <span id={errId} className="text-xs text-red-700 dark:text-red-300">
          {error}
        </span>
      )}
    </div>
  );
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input({ className, ...p }, ref) {
  return <input ref={ref} className={cx(inputCls, className)} {...p} />;
});

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(function Select({ className, ...p }, ref) {
  return <select ref={ref} className={cx(inputCls, className)} {...p} />;
});

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(function Textarea({ className, ...p }, ref) {
  return <textarea ref={ref} className={cx(inputCls, className)} {...p} />;
});

export function Checkbox({ label, ...p }: InputHTMLAttributes<HTMLInputElement> & { label: ReactNode }) {
  return (
    <label className="inline-flex items-start gap-2 text-sm">
      <input type="checkbox" className="mt-0.5 h-4 w-4 rounded border-slate-400 text-brand-700 focus:ring-brand-600" {...p} />
      <span>{label}</span>
    </label>
  );
}

export function KV({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
      {items.map(([k, v]) => (
        <div key={k}>
          <dt className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">{k}</dt>
          <dd className="mt-0.5 text-sm">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Mono({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span title={title} className="break-all font-mono text-xs">
      {children}
    </span>
  );
}

/** Таблиця з прокручуванням на вузьких екранах і підписом для зчитувачів екрана. */
export function Table({ caption, head, children }: { caption: string; head: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
      <table className="min-w-full divide-y divide-slate-200 text-sm dark:divide-slate-800">
        <caption className="sr-only">{caption}</caption>
        <thead className="bg-slate-50 dark:bg-slate-800/60">
          <tr>
            {head.map((h, i) => (
              <th key={i} scope="col" className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-slate-600 dark:text-slate-300">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">{children}</tbody>
      </table>
    </div>
  );
}

export function Td({ children, className }: { children?: ReactNode; className?: string }) {
  return <td className={cx("px-3 py-2 align-top", className)}>{children}</td>;
}
