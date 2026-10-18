/** Реєстрація документа: файл → сховище університету → OCR → реквізити від локальної LLM з оцінкою впевненості. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { documentsApi } from "../../api/endpoints";
import type { IngestResponse } from "../../api/types";
import { useApp } from "../../app/context";
import { useTitle } from "../../app/Layout";
import { fmtSize } from "../../domain/format";
import { Confidence, Status } from "../../components/status";
import { Button, Card, ErrorBox, Field, Input, KV, Notice, PageHeader, Select, Spinner } from "../../components/ui";

const ACCEPT = "application/pdf,image/png,image/jpeg,image/tiff,image/webp,.pdf,.png,.jpg,.jpeg,.tif,.tiff,.webp";
const MAX_BYTES = 50 * 1024 * 1024;

export function UploadPage() {
  useTitle("Реєстрація документа");
  const { me, dict, threshold, L } = useApp();
  const qc = useQueryClient();
  const units = me.permissions.upload_all_units ? dict.org_units : dict.org_units.filter((u) => me.permissions.upload_unit_ids.includes(u.id));
  const [file, setFile] = useState<File | null>(null);
  const [unit, setUnit] = useState(me.primary_org_unit_id && units.some((u) => u.id === me.primary_org_unit_id) ? me.primary_org_unit_id : units[0]?.id ?? "");
  const [direction, setDirection] = useState("");
  const [access, setAccess] = useState("");
  const [corr, setCorr] = useState("");
  const [fileErr, setFileErr] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const labels = dict.labels as Record<string, Record<string, string>>;

  const ingest = useMutation({
    mutationFn: () => {
      const fd = new FormData();
      fd.append("file", file!);
      fd.append("org_unit_id", unit);
      if (direction) fd.append("direction", direction);
      if (access) fd.append("access_level", access);
      if (direction === "incoming" && corr.trim()) fd.append("correspondent_name", corr.trim());
      return documentsApi.ingest(fd);
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["documents"] });
      void qc.invalidateQueries({ queryKey: ["review-queue"] });
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
    },
  });

  if (!units.length)
    return (
      <>
        <PageHeader title="Реєстрація документа" />
        <Notice tone="warn">У вас немає підрозділу, у якому можна реєструвати документи. Зверніться до адміністратора.</Notice>
      </>
    );

  return (
    <>
      <PageHeader
        title="Реєстрація документа"
        lead={<>ШІ визначить вид документа і вилучить реквізити за ДСТУ 4163:2020. Значення з впевненістю нижче {Math.round(threshold * 100)}% потраплять у чергу ручної перевірки: без вашого підтвердження вони не стануть реквізитами картки.</>}
      />
      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <form
            className="flex flex-col gap-4"
            onSubmit={(e) => {
              e.preventDefault();
              if (!file) return setFileErr("Оберіть файл");
              if (file.size > MAX_BYTES) return setFileErr(`Файл завеликий (${fmtSize(file.size)}), максимум 50 МБ`);
              if (direction === "incoming" && !corr.trim()) return;
              ingest.mutate();
            }}
          >
            <Field label="Файл документа" hint="PDF або скан/фото (PNG, JPEG, TIFF, WebP) до 50 МБ" error={fileErr} required>
              {(p) => (
                <Input {...p} ref={fileInput} type="file" accept={ACCEPT}
                  onChange={(e) => { setFile(e.target.files?.[0] ?? null); setFileErr(null); }} />
              )}
            </Field>
            <Field label="Підрозділ реєстрації" required>
              {(p) => <Select {...p} value={unit} onChange={(e) => setUnit(e.target.value)}>{units.map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}</Select>}
            </Field>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Вид кореспонденції">
                {(p) => (
                  <Select {...p} value={direction} onChange={(e) => setDirection(e.target.value)}>
                    <option value="">Визначити за видом документа</option>
                    {Object.entries(labels.DocumentDirection ?? {}).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                  </Select>
                )}
              </Field>
              <Field label="Гриф доступу" hint="Посилюється до «Персональні дані», якщо ШІ знайде ПД">
                {(p) => (
                  <Select {...p} value={access} onChange={(e) => setAccess(e.target.value)}>
                    <option value="">За видом документа</option>
                    {Object.entries(labels.AccessLevel ?? {}).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                  </Select>
                )}
              </Field>
            </div>
            {direction === "incoming" && (
              <Field label="Кореспондент" hint="Обов'язково для вхідних" required>
                {(p) => <Input {...p} maxLength={512} placeholder="Міністерство освіти і науки України" value={corr} onChange={(e) => setCorr(e.target.value)} />}
              </Field>
            )}
            <div className="flex flex-wrap items-center gap-3">
              <Button type="submit" variant="primary" busy={ingest.isPending}>Зареєструвати і розпізнати</Button>
              <span className="text-xs text-slate-600 dark:text-slate-400">Текст розпізнається локально, дані не залишають периметр університету.</span>
            </div>
          </form>
        </Card>
        <div aria-live="polite">
          {ingest.isPending && (
            <Card><Spinner label="Зберігаємо оригінал, розпізнаємо текст і вилучаємо реквізити. Це може тривати кілька хвилин…" /></Card>
          )}
          {ingest.error && <ErrorBox error={ingest.error} />}
          {ingest.data && <IngestResult r={ingest.data} L={L} typeName={(c) => dict.document_types.find((t) => t.code === c)?.name ?? c} />}
        </div>
      </div>
    </>
  );
}

function IngestResult({ r, L, typeName }: { r: IngestResponse; L: (e: string, v: string) => string; typeName: (c: string) => string }) {
  const headline = {
    processed: "Документ зареєстровано, реквізити вилучено з достатньою впевненістю. Перенесіть їх у картку після перегляду.",
    needs_review: "Документ зареєстровано. Частина реквізитів потребує вашої перевірки.",
    manual_entry_required: "Документ зареєстровано, але реквізити не вилучено: введіть їх вручну в картці.",
  }[r.status as "processed" | "needs_review" | "manual_entry_required"];
  return (
    <Card className="space-y-4">
      <Notice tone={r.status === "processed" ? "ok" : "warn"}>{headline}</Notice>
      <KV items={[
        ["Вид", typeName(r.document_type_code)],
        ["Гриф", <Status key="a" e="AccessLevel" v={r.access_level} />],
        ["Напрям", L("DocumentDirection", r.direction)],
        ["Впевненість ШІ", <Confidence key="c" value={r.confidence} />],
        ["Розпізнавання", `${r.ocr.pages_processed} із ${r.ocr.page_count} стор., ${r.ocr.used_ocr ? "OCR" : "текстовий шар PDF"}`],
      ]} />
      {(r.review_reasons ?? []).length > 0 && (
        <div className="text-sm"><b>Що перевірити:</b><ul className="ml-5 list-disc">{(r.review_reasons ?? []).map((x) => <li key={x}>{x}</li>)}</ul></div>
      )}
      {(r.warnings ?? []).length > 0 && <Notice tone="warn">{(r.warnings ?? []).join(" ")}</Notice>}
      {r.duplicate_of && <Notice tone="warn">Такий самий файл уже зареєстровано раніше.</Notice>}
      <div className="flex flex-wrap gap-2">
        <Link to={`/documents/${r.document_id}`} className="rounded-md bg-brand-700 px-4 py-2 text-sm font-medium text-white no-underline hover:bg-brand-800 hover:no-underline">Відкрити картку</Link>
        {r.duplicate_of && <Link to={`/documents/${r.duplicate_of}`} className="rounded-md border px-4 py-2 text-sm">Відкрити попередній документ</Link>}
      </div>
    </Card>
  );
}
