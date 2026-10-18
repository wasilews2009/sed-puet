/**
 * Нормативний аудит поточної версії: вердикт, зауваження з цитатами пунктів Статуту, положень ПУЕТ і наказів МОН.
 * Людина розглядає кожне зауваження (прийняти / виправлено / хибне), і лише тоді — підтверджує звіт.
 */
import { useState } from "react";
import { documentsApi } from "../../api/endpoints";
import type { DocumentCard, FindingOut } from "../../api/types";
import { useApp } from "../../app/context";
import { fmtDate, fmtDateTime } from "../../domain/format";
import { toneOf } from "../../domain/labels";
import { useAskReason } from "../../components/dialog";
import { FindingBox } from "../../components/findings";
import { Confidence, Status } from "../../components/status";
import { ActionButton, Field, Input, Notice } from "../../components/ui";
import { useCardAction } from "./useCard";

export function ComplianceTab({ card }: { card: DocumentCard }) {
  const { L } = useApp();
  const { run } = useCardAction(card);
  const ask = useAskReason();
  const [asOf, setAsOf] = useState("");
  const c = card.compliance;
  const current = card.versions.find((v) => v.id === card.current_version_id);
  const open = (c?.findings ?? []).filter((f) => f.status === "open").length;

  const runCheck = card.permissions.check_compliance ? (
    <div className="flex flex-wrap items-end gap-3">
      <Field label="Норми станом на" hint="Порожньо — на сьогодні">
        {(p) => <Input {...p} type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} />}
      </Field>
      <ActionButton variant={c ? "secondary" : "primary"} onAction={() => run(() => documentsApi.runCompliance(card.id, asOf || null), "Перевірку завершено")}>
        {c ? "Перевірити повторно" : "Перевірити на відповідність нормам"}
      </ActionButton>
    </div>
  ) : null;

  if (!c)
    return (
      <div className="space-y-3">
        <p className="text-sm">Перевірка шукає пункти Статуту ПУЕТ, положень університету, законів і наказів МОН, яким може суперечити проєкт, і наводить дослівні цитати норм.</p>
        {current && !current.has_text && <Notice tone="warn">У версії немає розпізнаного тексту — перевірка неможлива.</Notice>}
        {runCheck}
      </div>
    );

  return (
    <div className="space-y-4">
      <Notice tone={toneOf("ComplianceVerdict", c.verdict)} title={L("ComplianceVerdict", c.verdict)}>{c.summary}</Notice>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-slate-600 dark:text-slate-400">
        <span>Норми на {fmtDate(c.regulations_as_of)}</span>
        <span>актів розглянуто: {c.sources_considered}</span>
        <span>модель {c.run.model}</span>
        <span>впевненість <Confidence value={c.run.confidence} /></span>
        <Status e="HumanReviewStatus" v={c.run.review_status} />
        <span>{fmtDateTime(c.created_at)}</span>
      </div>
      {c.document_version_id !== card.current_version_id && <Notice tone="warn">Звіт стосується попередньої версії документа. Перевірте поточну версію повторно.</Notice>}
      {c.review_reasons.length > 0 && (
        <div className="text-sm"><b>Чому потрібна перевірка людиною:</b><ul className="ml-5 list-disc">{c.review_reasons.map((r) => <li key={r}>{r}</li>)}</ul></div>
      )}
      {(c.run.warnings ?? []).map((w) => <Notice key={w} tone="warn">{w}</Notice>)}
      {c.findings.length ? c.findings.map((f) => <Finding key={f.id} card={card} f={f} />) : <p className="text-sm text-slate-600">Зауважень немає.</p>}
      <div className="flex flex-wrap gap-3">
        {runCheck}
        {card.permissions.review && c.run.review_status === "pending" && (
          <>
            <ActionButton variant="ok" onAction={() => {
              if (open) throw new Error(`Спершу розгляньте всі відкриті зауваження (${open})`);
              return run(() => documentsApi.reviewRun(card.id, c.run.id, "confirm", null), "Звіт підтверджено");
            }}>Підтвердити звіт</ActionButton>
            <ActionButton variant="danger" onAction={async () => {
              const reason = await ask({ title: "Відхилити звіт ШІ", label: "Чому звіт хибний (буде збережено в журналі)", confirmText: "Відхилити звіт" });
              if (reason) await run(() => documentsApi.reviewRun(card.id, c.run.id, "reject", reason), "Звіт відхилено");
            }}>Відхилити звіт</ActionButton>
          </>
        )}
      </div>
    </div>
  );
}

function Finding({ card, f }: { card: DocumentCard; f: FindingOut }) {
  const { L } = useApp();
  const { run } = useCardAction(card);
  const ask = useAskReason();
  const resolve = (st: "accepted" | "fixed" | "dismissed", comment: string | null) =>
    run(() => documentsApi.resolveFinding(card.id, f.id, st, comment), `Зауваження: ${L("FindingStatus", st).toLowerCase()}`);
  return (
    <FindingBox
      severity={f.severity} category={f.category} title={f.title} explanation={f.explanation}
      recommendation={f.recommendation} fragment={f.fragment_text} confidence={f.confidence}
      citations={f.citations} status={f.status}
      footer={f.status !== "open" ? (
        <div className="text-xs text-slate-600 dark:text-slate-400">
          {L("FindingStatus", f.status)}: {f.resolved_by?.name}, {fmtDateTime(f.resolved_at)}{f.resolution_comment ? ` — ${f.resolution_comment}` : ""}
        </div>
      ) : card.permissions.review ? (
        <div className="flex flex-wrap gap-2">
          <ActionButton size="sm" onAction={() => resolve("accepted", null)}>Прийняти до виправлення</ActionButton>
          <ActionButton size="sm" variant="ok" onAction={() => resolve("fixed", null)}>Виправлено</ActionButton>
          <ActionButton size="sm" variant="danger" onAction={async () => {
            const reason = await ask({ title: "Хибне спрацювання", label: "Чому зауваження хибне (буде збережено в журналі)", confirmText: "Позначити хибним" });
            if (reason) await resolve("dismissed", reason);
          }}>Хибне спрацювання</ActionButton>
        </div>
      ) : null}
    />
  );
}
