/** Перевірка тексту проєкту до реєстрації (без збереження): суперечності чинним нормам з цитатами. */
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { regulationsApi } from "../../api/endpoints";
import { useApp } from "../../app/context";
import { useTitle } from "../../app/Layout";
import { toneOf } from "../../domain/labels";
import { FindingBox } from "../../components/findings";
import { Confidence, Status } from "../../components/status";
import { Button, Card, ErrorBox, Field, Input, Loading, Notice, PageHeader, Table, Td, Textarea } from "../../components/ui";

export function DraftCheckPage() {
  useTitle("Перевірка проєкту");
  const { L } = useApp();
  const [text, setText] = useState("");
  const [asOf, setAsOf] = useState("");
  const check = useMutation({ mutationFn: () => regulationsApi.checkText(text, asOf || null) });
  const r = check.data;

  return (
    <>
      <PageHeader title="Перевірка проєкту документа" lead="Вставте текст проєкту наказу, положення чи службової записки: ШІ знайде пункти, що можуть суперечити Статуту, положенням ПУЕТ або наказам МОН, і наведе цитати. Текст не зберігається в СЕД." />
      <Card>
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); if (text.trim().length >= 20) check.mutate(); }}>
          <Field label="Текст проєкту" required hint="Персональні дані маскуються перед передачею моделі">
            {(p) => <Textarea {...p} rows={12} value={text} onChange={(e) => setText(e.target.value)} />}
          </Field>
          <div className="flex flex-wrap items-end gap-3">
            <Field label="…або з файлу .txt">{(p) => <Input {...p} type="file" accept=".txt,text/plain" onChange={async (e) => { const f = e.target.files?.[0]; if (f) setText(await f.text()); }} />}</Field>
            <Field label="Норми станом на">{(p) => <Input {...p} type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} />}</Field>
            <Button type="submit" variant="primary" busy={check.isPending} disabled={text.trim().length < 20}>Перевірити</Button>
          </div>
        </form>
      </Card>
      <div className="mt-4 space-y-4" aria-live="polite">
        {check.isPending && <Loading label="Пошук норм і оцінка кожного пункту проєкту…" />}
        {check.error && <ErrorBox error={check.error} />}
        {r && (
          <>
            <Notice tone={toneOf("ComplianceVerdict", r.verdict)} title={r.verdict_label}>{r.summary}</Notice>
            <div className="flex flex-wrap items-center gap-3 text-sm text-slate-600 dark:text-slate-400">
              <span>Впевненість <Confidence value={r.confidence} /></span>
              <span>Норми на {r.regulations_as_of}</span>
              <span>актів розглянуто: {r.sources_considered}</span>
              {r.requires_human_review && <Status e="HumanReviewStatus" v="pending" />}
            </div>
            {r.review_reasons.length > 0 && <div className="text-sm"><b>Чому потрібна перевірка людиною:</b><ul className="ml-5 list-disc">{r.review_reasons.map((x) => <li key={x}>{x}</li>)}</ul></div>}
            {(r.warnings ?? []).map((w) => <Notice key={w} tone="warn">{w}</Notice>)}
            {r.findings.map((f) => (
              <FindingBox key={f.ordinal} severity={f.severity} category={f.category} title={f.title} explanation={f.explanation}
                recommendation={f.recommendation} fragment={f.fragment.text} confidence={f.confidence} citations={f.citations} />
            ))}
            {r.provisions.length > 0 && (
              <section aria-labelledby="prov-h">
                <h2 id="prov-h" className="mb-2 font-semibold">Покриття пунктів проєкту нормами</h2>
                <Table caption="Покриття пунктів проєкту" head={["Пункт проєкту", "Норми", "Знайдено норм", "Без зауважень"]}>
                  {r.provisions.map((p) => (
                    <tr key={p.provision_id}>
                      <Td>{p.label}</Td>
                      <Td><Status e="Coverage" v={p.coverage} /></Td>
                      <Td className="text-xs">{p.norms_considered.join("; ") || "—"}</Td>
                      <Td>{p.assessed ? <Confidence value={p.no_issue_confidence ?? null} /> : <span className="text-xs text-slate-500">не оцінено</span>}</Td>
                    </tr>
                  ))}
                </Table>
              </section>
            )}
            {r.disclaimer && <p className="text-xs text-slate-600 dark:text-slate-400">{r.disclaimer}</p>}
            <p className="text-xs text-slate-500">{L("ComplianceVerdict", r.verdict)}: результат ШІ без юридичної сили.</p>
          </>
        )}
      </div>
    </>
  );
}
