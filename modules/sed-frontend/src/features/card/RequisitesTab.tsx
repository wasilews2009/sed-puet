/**
 * Реквізити від ШІ з ручною валідацією (Human-in-the-loop):
 *  - поле з впевненістю < порогу має статус «Очікує перевірки»: підтвердити / виправити / відхилити;
 *  - впевнені значення в картку все одно переносить людина (одна кнопка);
 *  - ШІ не впорався — ручне введення.
 * Кожна дія передає row_version: якщо картку змінив хтось інший, сервер поверне 409.
 */
import { useState } from "react";
import { documentsApi } from "../../api/endpoints";
import type { DocumentCard, ExtractedField } from "../../api/types";
import { useApp } from "../../app/context";
import { fmtDateTime } from "../../domain/format";
import { MANUAL_KEYS, isPerson, parsePeople, peopleText, requisiteText } from "../../domain/requisites";
import { Confidence, Status } from "../../components/status";
import { ActionButton, Button, Field, Input, Notice, Table, Td, Textarea } from "../../components/ui";
import { useCardAction } from "./useCard";

export function RequisitesTab({ card }: { card: DocumentCard }) {
  const { L, threshold, fieldLabel } = useApp();
  const { run } = useCardAction(card);
  const x = card.extraction;
  const can = card.permissions.review;
  const show = (key: string, v: unknown) => requisiteText(v, key === "document_type" ? (c) => L("DocumentKind", c) : undefined);
  const confident = (x?.fields ?? []).filter((f) => f.review_status === "not_required" && f.extracted_value != null && f.extracted_value !== "");
  const confirmed = Object.entries((card.requisites ?? {}) as Record<string, unknown>);
  const showManual = can && (!x || x.run.status === "failed" || x.run.review_status === "pending");

  return (
    <div className="space-y-5">
      {!x ? (
        <Notice>Реквізити ще не вилучалися для цієї версії.</Notice>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-slate-600 dark:text-slate-400">
            <span>Модель: <b>{x.run.model}</b></span>
            <span>Загальна впевненість: <Confidence value={x.run.confidence} /></span>
            <span>Перевірка: <Status e="HumanReviewStatus" v={x.run.review_status} /></span>
            {x.ocr_confidence != null && <span>Якість розпізнавання: <Confidence value={x.ocr_confidence} /></span>}
          </div>
          {x.run.status === "failed" && <Notice tone="warn">ШІ не вилучив реквізити: {x.run.error_message || "модель недоступна"}. Введіть їх вручну нижче.</Notice>}
          {(x.run.warnings ?? []).map((w) => <Notice key={w} tone="warn">{w}</Notice>)}
          {x.fields.length > 0 && (
            <Table caption="Реквізити, вилучені ШІ" head={["Реквізит", "Значення від ШІ", "Впевненість", "Перевірка", "Дії"]}>
              {x.fields.map((f) => <FieldRow key={f.id} card={card} f={f} show={show} />)}
            </Table>
          )}
          {can && confident.length > 0 && (
            <div className="flex flex-wrap items-center gap-3">
              <ActionButton variant="primary" onAction={() => run(async () => {
                const r = await documentsApi.acceptConfident(card.id, card.row_version);
                return r;
              }, `Перенесено в картку: ${confident.length}`)}>
                Перенести в картку впевнені значення ({confident.length})
              </ActionButton>
              <span className="text-xs text-slate-600 dark:text-slate-400">Значення з впевненістю від {Math.round(threshold * 100)}% можна прийняти разом; решту перевірте окремо.</span>
            </div>
          )}
        </>
      )}
      <section aria-labelledby="confirmed-req">
        <h3 id="confirmed-req" className="mb-2 font-semibold">Реквізити картки (підтверджені людиною)</h3>
        {confirmed.length ? (
          <dl className="grid gap-3 sm:grid-cols-2">
            {confirmed.map(([k, v]) => (
              <div key={k}><dt className="text-xs uppercase tracking-wide text-slate-500">{fieldLabel(k)}</dt><dd className="text-sm">{show(k, v)}</dd></div>
            ))}
          </dl>
        ) : <p className="text-sm text-slate-600 dark:text-slate-400">Поки жодного: значення ШІ стають реквізитами картки лише після підтвердження.</p>}
      </section>
      {showManual && <ManualForm card={card} />}
    </div>
  );
}

function FieldRow({ card, f, show }: { card: DocumentCard; f: ExtractedField; show: (k: string, v: unknown) => string }) {
  const { run } = useCardAction(card);
  const [mode, setMode] = useState<"none" | "correct" | "reject">("none");
  const can = card.permissions.review;
  const review = (body: { decision: "confirm" | "correct" | "reject"; value?: unknown; comment?: string | null }, msg: string) =>
    run(() => documentsApi.reviewField(card.id, f.id, { ...body, row_version: card.row_version }), `«${f.label}»: ${msg}`);

  return (
    <tr className={f.review_status === "pending" ? "bg-amber-50/60 dark:bg-amber-950/30" : undefined}>
      <Td><b>{f.label}</b>{f.page_no && <div className="text-xs text-slate-500">стор. {f.page_no}</div>}</Td>
      <Td>
        <div className="whitespace-pre-wrap">{show(f.key, f.extracted_value)}</div>
        {f.review_status === "corrected" && <div className="mt-1 text-xs">Виправлено: <b>{show(f.key, f.validated_value)}</b></div>}
        {f.issues.length > 0 && <ul className="mt-1 list-disc pl-4 text-xs text-slate-600 dark:text-slate-400">{f.issues.map((i) => <li key={i}>{i}</li>)}</ul>}
        {mode === "correct" && (
          <CorrectEditor fieldKey={f.key} value={f.validated_value ?? f.extracted_value} onCancel={() => setMode("none")}
            onSave={async (value, comment) => { await review({ decision: "correct", value, comment }, "виправлено"); setMode("none"); }} />
        )}
        {mode === "reject" && <RejectEditor onCancel={() => setMode("none")} onSave={async (comment) => { await review({ decision: "reject", comment }, "відхилено"); setMode("none"); }} />}
      </Td>
      <Td><Confidence value={f.confidence} /></Td>
      <Td>
        <Status e="HumanReviewStatus" v={f.review_status} />
        {f.reviewed_by && <div className="mt-1 text-xs text-slate-500">{f.reviewed_by.name}, {fmtDateTime(f.reviewed_at)}</div>}
        {f.review_comment && <div className="text-xs">{f.review_comment}</div>}
      </Td>
      <Td className="whitespace-nowrap">
        {can && mode === "none" && (
          <div className="flex flex-wrap gap-1">
            {f.review_status === "pending" && <ActionButton size="sm" variant="ok" onAction={() => review({ decision: "confirm" }, "підтверджено")}>Підтвердити</ActionButton>}
            <Button size="sm" onClick={() => setMode("correct")}>Виправити</Button>
            {f.review_status === "pending" && <Button size="sm" variant="danger" onClick={() => setMode("reject")}>Відхилити</Button>}
          </div>
        )}
      </Td>
    </tr>
  );
}

/** Редактор значення залежно від реквізиту: дата, особа, список осіб, текст. */
function useValueEditor(key: string, value: unknown) {
  const person = isPerson(value) ? value : { full_name: "", position: "", org_unit: "" };
  const [text, setText] = useState(key === "interested_parties" ? peopleText(value) : typeof value === "string" ? value : "");
  const [date, setDate] = useState(typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : "");
  const [p, setP] = useState({ full_name: person.full_name, position: person.position ?? "", org_unit: person.org_unit ?? "" });
  const read = (): unknown => {
    if (key === "document_date") return date;
    if (key === "author") return { full_name: p.full_name.trim(), position: p.position.trim() || null, org_unit: p.org_unit.trim() || null };
    if (key === "interested_parties") return parsePeople(text);
    return text;
  };
  const node =
    key === "document_date" ? (
      <Field label="Дата">{(a) => <Input {...a} type="date" value={date} onChange={(e) => setDate(e.target.value)} />}</Field>
    ) : key === "author" ? (
      <div className="grid gap-2 sm:grid-cols-3">
        <Field label="Ім'я та прізвище">{(a) => <Input {...a} value={p.full_name} onChange={(e) => setP({ ...p, full_name: e.target.value })} />}</Field>
        <Field label="Посада">{(a) => <Input {...a} value={p.position} onChange={(e) => setP({ ...p, position: e.target.value })} />}</Field>
        <Field label="Підрозділ / організація">{(a) => <Input {...a} value={p.org_unit} onChange={(e) => setP({ ...p, org_unit: e.target.value })} />}</Field>
      </div>
    ) : (
      <Field label="Значення" hint={key === "interested_parties" ? "Одна особа в рядку: ПІБ, посада" : undefined}>
        {(a) => <Textarea {...a} rows={key === "summary" ? 4 : 2} value={text} onChange={(e) => setText(e.target.value)} />}
      </Field>
    );
  return { node, read };
}

function CorrectEditor({ fieldKey, value, onSave, onCancel }: { fieldKey: string; value: unknown; onSave: (v: unknown, c: string | null) => Promise<void>; onCancel: () => void }) {
  const ed = useValueEditor(fieldKey, value);
  const [comment, setComment] = useState("");
  return (
    <div className="mt-2 space-y-2 rounded-md border border-slate-200 p-3 dark:border-slate-700">
      {ed.node}
      <Field label="Коментар (необов'язково)">{(a) => <Input {...a} value={comment} onChange={(e) => setComment(e.target.value)} />}</Field>
      <div className="flex gap-2">
        <ActionButton size="sm" variant="primary" onAction={() => onSave(ed.read(), comment.trim() || null)}>Зберегти виправлення</ActionButton>
        <Button size="sm" onClick={onCancel}>Скасувати</Button>
      </div>
    </div>
  );
}

function RejectEditor({ onSave, onCancel }: { onSave: (c: string) => Promise<void>; onCancel: () => void }) {
  const [comment, setComment] = useState("");
  return (
    <div className="mt-2 space-y-2 rounded-md border border-red-200 p-3 dark:border-red-900">
      <Field label="Чому значення хибне" required>{(a) => <Input {...a} value={comment} onChange={(e) => setComment(e.target.value)} />}</Field>
      <div className="flex gap-2">
        <ActionButton size="sm" variant="danger" onAction={() => { if (!comment.trim()) throw new Error("Вкажіть причину відхилення"); return onSave(comment.trim()); }}>Відхилити значення</ActionButton>
        <Button size="sm" onClick={onCancel}>Скасувати</Button>
      </div>
    </div>
  );
}

function ManualForm({ card }: { card: DocumentCard }) {
  const { fieldLabel } = useApp();
  const { run } = useCardAction(card);
  const req = (card.requisites ?? {}) as Record<string, unknown>;
  const editors = MANUAL_KEYS.map((k) => ({ key: k, ...useValueEditor(k, req[k]) })); // порядок полів сталий
  const failed = card.extraction?.run.status === "failed" || !card.extraction;
  return (
    <details open={failed} className="rounded-md border border-slate-200 p-3 dark:border-slate-700">
      <summary className="cursor-pointer font-semibold">Ввести реквізити вручну</summary>
      <div className="mt-3 space-y-3">
        {editors.map((e) => (
          <fieldset key={e.key} className="space-y-1">
            <legend className="text-sm font-medium">{fieldLabel(e.key)}</legend>
            {e.node}
          </fieldset>
        ))}
        <ActionButton variant="primary" onAction={() => {
          const values: Record<string, unknown> = {};
          for (const e of editors) {
            const v = e.read();
            if (v && (typeof v !== "object" || (isPerson(v) && v.full_name))) values[e.key] = v;
          }
          if (!Object.keys(values).length) throw new Error("Заповніть хоча б один реквізит");
          return run(() => documentsApi.saveRequisites(card.id, values, card.row_version), "Реквізити збережено");
        }}>Зберегти реквізити</ActionButton>
      </div>
    </details>
  );
}
