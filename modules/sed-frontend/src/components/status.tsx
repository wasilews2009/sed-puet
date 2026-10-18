/** Позначки статусів і впевненості ШІ. */
import { useApp } from "../app/context";
import { fmtPercent, toScore } from "../domain/format";
import { toneOf } from "../domain/labels";
import { Pill } from "./ui";

export function Status({ e, v }: { e: string; v: string | null | undefined }) {
  const { L } = useApp();
  if (v === null || v === undefined) return <span className="text-slate-500">—</span>;
  return <Pill tone={toneOf(e, v)}>{L(e, v)}</Pill>;
}

/**
 * Впевненість ШІ: смужка + відсоток. Нижче порогу (0.85) — попередження «потребує перевірки людиною».
 * Колір не єдиний носій змісту: є текст і aria-label.
 */
export function Confidence({ value }: { value: string | number | null | undefined }) {
  const { threshold } = useApp();
  const v = toScore(value);
  if (v === null) return <span className="text-sm text-slate-500">—</span>;
  const low = v < threshold;
  return (
    <span
      className="inline-flex items-center gap-2"
      aria-label={`Впевненість ШІ ${fmtPercent(v)}${low ? ", нижче порогу — потрібна перевірка людиною" : ""}`}
      title={low ? `Нижче порогу ${fmtPercent(threshold)}: потрібна перевірка людиною` : undefined}
    >
      <span aria-hidden="true" className="h-1.5 w-14 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700">
        <span className={`block h-full ${low ? "bg-amber-500" : "bg-emerald-600"}`} style={{ width: `${Math.round(v * 100)}%` }} />
      </span>
      <span className={`text-sm tabular-nums ${low ? "font-semibold text-amber-800 dark:text-amber-300" : ""}`}>
        {fmtPercent(v)}
        {low && <span aria-hidden="true"> ⚠</span>}
      </span>
    </span>
  );
}
