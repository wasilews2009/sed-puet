/** Зауваження нормативного аудиту з дослівними цитатами норм — спільне для картки і перевірки проєкту. */
import type { ReactNode } from "react";
import { useApp } from "../app/context";
import { Confidence, Status } from "./status";
import { Pill, cx } from "./ui";

export interface CitationLike {
  citation_label: string;
  source_title: string;
  section_ref?: string | null;
  quoted_text: string;
  quote_verified: boolean;
  source_status: string;
  is_sample?: boolean;
}

export function Citation({ c }: { c: CitationLike }) {
  const { L } = useApp();
  return (
    <figure className="mt-2 text-sm">
      <figcaption className="flex flex-wrap items-center gap-2">
        <b>{c.citation_label}</b>
        {c.quote_verified ? <Pill tone="ok">цитату звірено з текстом акта</Pill> : <Pill tone="warn">цитату не знайдено дослівно</Pill>}
        {c.source_status !== "in_force" && <Pill tone="danger">акт: {L("RegulatoryStatus", c.source_status)}</Pill>}
        {c.is_sample && <Pill tone="warn">зразок тексту, не офіційна редакція</Pill>}
      </figcaption>
      <blockquote className="mt-1">{c.quoted_text}</blockquote>
    </figure>
  );
}

export function FindingBox({
  severity,
  category,
  title,
  explanation,
  recommendation,
  fragment,
  confidence,
  citations,
  status,
  footer,
}: {
  severity: string;
  category: string;
  title: string;
  explanation: string;
  recommendation?: string | null;
  fragment?: string | null;
  confidence: string;
  citations: CitationLike[];
  status?: string;
  footer?: ReactNode;
}) {
  const { L } = useApp();
  const border = severity === "critical" ? "border-l-red-600" : severity === "major" ? "border-l-amber-500" : "border-l-sky-500";
  return (
    <article className={cx("rounded-md border border-l-4 border-slate-200 p-4 dark:border-slate-700", border)}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Status e="FindingSeverity" v={severity} />
          <Pill>{L("FindingCategory", category)}</Pill>
          <h4 className="font-semibold">{title}</h4>
        </div>
        <div className="flex items-center gap-2 text-sm">
          <Confidence value={confidence} />
          {status && <Status e="FindingStatus" v={status} />}
        </div>
      </div>
      <p className="mt-2 text-sm">{explanation}</p>
      {fragment && <p className="mt-2 text-sm">Фрагмент проєкту: <mark className="rounded bg-amber-100 px-1 dark:bg-amber-900/60 dark:text-amber-50">{fragment}</mark></p>}
      {recommendation && <p className="mt-2 text-sm"><b>Рекомендація:</b> {recommendation}</p>}
      {citations.map((c, i) => <Citation key={i} c={c} />)}
      {footer && <div className="mt-3">{footer}</div>}
    </article>
  );
}
