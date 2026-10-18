/**
 * Нормативна база: гібридний пошук (dense + BM25) по Статуту, положеннях ПУЕТ, наказах МОН.
 * Адміністратор корпусу (ректор, проректор) додає акт текстом і змінює його статус/строки чинності.
 */
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { regulationsApi } from "../../api/endpoints";
import type { ActIn, RegulatoryLevel, RegulatoryStatus, SearchHit } from "../../api/types";
import { useApp } from "../../app/context";
import { useTitle } from "../../app/Layout";
import { fallbackLabels } from "../../domain/labels";
import { Status } from "../../components/status";
import { useToast } from "../../components/toast";
import { Modal } from "../../components/dialog";
import { ActionButton, Button, Card, Empty, ErrorBox, Field, Input, Loading, Notice, PageHeader, Pill, Select, Textarea } from "../../components/ui";

export function RegulationsPage() {
  useTitle("Нормативна база");
  const { me, L } = useApp();
  const [q, setQ] = useState("");
  const [asOf, setAsOf] = useState("");
  const [k, setK] = useState(10);
  const [edit, setEdit] = useState<SearchHit | null>(null);
  const search = useMutation({ mutationFn: () => regulationsApi.search(q.trim(), asOf || undefined, k) });

  return (
    <>
      <PageHeader title="Нормативна база" lead="Пошук пунктів Статуту ПУЕТ, положень університету, законів і наказів МОН за змістом і ключовими словами. Результат — дослівні фрагменти з посиланням на пункт." />
      <Card>
        <form role="search" className="grid gap-3 md:grid-cols-[1fr_auto_auto_auto] md:items-end"
          onSubmit={(e) => { e.preventDefault(); if (q.trim().length >= 3) search.mutate(); }}>
          <Field label="Що шукати" hint="Щонайменше 3 символи: «строк погодження наказу», «повноваження вченої ради»">
            {(p) => <Input {...p} type="search" value={q} minLength={3} onChange={(e) => setQ(e.target.value)} />}
          </Field>
          <Field label="Чинні станом на">{(p) => <Input {...p} type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} />}</Field>
          <Field label="Результатів">{(p) => <Select {...p} value={k} onChange={(e) => setK(Number(e.target.value))}>{[5, 10, 20, 30].map((n) => <option key={n}>{n}</option>)}</Select>}</Field>
          <Button type="submit" variant="primary" busy={search.isPending}>Знайти</Button>
        </form>
      </Card>
      <div className="mt-4 space-y-3" aria-live="polite">
        {search.isPending && <Loading label="Пошук…" />}
        {search.error && <ErrorBox error={search.error} />}
        {search.data && (
          <>
            <div className="flex flex-wrap items-center gap-2 text-sm text-slate-600 dark:text-slate-400">
              Знайдено: {search.data.hits.length} · норми на {search.data.as_of} · <Status e="Coverage" v={search.data.coverage} />
            </div>
            {search.data.coverage === "none" && <Notice tone="warn">Відповідних норм у корпусі не знайдено. Висновок «суперечностей немає» на такій основі робити не можна.</Notice>}
            {search.data.hits.length === 0 ? <Empty>Нічого не знайдено.</Empty> : search.data.hits.map((h) => (
              <Card as="article" key={h.chunk_id}>
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <h2 className="font-semibold">{h.citation_label}</h2>
                    <div className="text-xs text-slate-600 dark:text-slate-400">{h.source_title} · {L("RegulatoryLevel", h.level)}</div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Status e="RegulatoryStatus" v={h.status} />
                    {h.is_sample && <Pill tone="warn">зразок, не офіційна редакція</Pill>}
                    <span className="text-xs text-slate-500" title="Ранг злиття dense + BM25">релевантність {h.fused_score.toFixed(3)}</span>
                    {me.permissions.corpus_admin && <Button size="sm" onClick={() => setEdit(h)}>Статус акта</Button>}
                  </div>
                </div>
                <blockquote className="mt-2 whitespace-pre-wrap text-sm">{h.content}</blockquote>
              </Card>
            ))}
          </>
        )}
      </div>
      {me.permissions.corpus_admin && <AddAct />}
      {edit && <EditStatus hit={edit} onClose={() => setEdit(null)} />}
    </>
  );
}

function ActFields({ act, set }: { act: ActIn; set: (a: ActIn) => void }) {
  const levels = fallbackLabels("RegulatoryLevel");
  const statuses = fallbackLabels("RegulatoryStatus");
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <Field label="Назва акта" required>{(p) => <Input {...p} value={act.title} onChange={(e) => set({ ...act, title: e.target.value })} />}</Field>
      <Field label="Коротка назва">{(p) => <Input {...p} value={act.short_title ?? ""} onChange={(e) => set({ ...act, short_title: e.target.value || null })} />}</Field>
      <Field label="Рівень" required>{(p) => <Select {...p} value={act.level} onChange={(e) => set({ ...act, level: e.target.value as RegulatoryLevel })}>{Object.entries(levels).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</Select>}</Field>
      <Field label="Ким прийнято" required>{(p) => <Input {...p} value={act.issuer} placeholder="Вчена рада ПУЕТ" onChange={(e) => set({ ...act, issuer: e.target.value })} />}</Field>
      <Field label="Статус">{(p) => <Select {...p} value={act.status ?? "in_force"} onChange={(e) => set({ ...act, status: e.target.value as RegulatoryStatus })}>{Object.entries(statuses).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</Select>}</Field>
      <Field label="Номер">{(p) => <Input {...p} value={act.doc_number ?? ""} onChange={(e) => set({ ...act, doc_number: e.target.value || null })} />}</Field>
      <Field label="Дата прийняття">{(p) => <Input {...p} type="date" value={act.adopted_on ?? ""} onChange={(e) => set({ ...act, adopted_on: e.target.value || null })} />}</Field>
      <Field label="Чинний з">{(p) => <Input {...p} type="date" value={act.effective_from ?? ""} onChange={(e) => set({ ...act, effective_from: e.target.value || null })} />}</Field>
      <Field label="Чинний до">{(p) => <Input {...p} type="date" value={act.effective_to ?? ""} onChange={(e) => set({ ...act, effective_to: e.target.value || null })} />}</Field>
      <Field label="Посилання на джерело">{(p) => <Input {...p} type="url" value={act.source_url ?? ""} onChange={(e) => set({ ...act, source_url: e.target.value || null })} />}</Field>
    </div>
  );
}

const EMPTY_ACT: ActIn = { title: "", level: "university", issuer: "", status: "in_force" };

function AddAct() {
  const toast = useToast();
  const [act, setAct] = useState<ActIn>(EMPTY_ACT);
  const [text, setText] = useState("");
  return (
    <details className="mt-6 rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
      <summary className="cursor-pointer font-semibold">Додати або переіндексувати акт (адміністратор корпусу)</summary>
      <div className="mt-4 space-y-3">
        <Notice>Текст акта має бути офіційною редакцією. Для сканів спершу розпізнайте текст (CLI модуля RAG, `extract-text`). Повторне додавання акта з тією самою назвою і номером створює нову редакцію індексу.</Notice>
        <ActFields act={act} set={setAct} />
        <Field label="Повний текст акта" required hint="Пункти мають бути пронумеровані: сегментатор ділить текст за ними">
          {(p) => <Textarea {...p} rows={10} value={text} onChange={(e) => setText(e.target.value)} />}
        </Field>
        <Field label="…або завантажити з файлу .txt">
          {(p) => <Input {...p} type="file" accept=".txt,text/plain" onChange={async (e) => { const f = e.target.files?.[0]; if (f) setText(await f.text()); }} />}
        </Field>
        <ActionButton variant="primary" onAction={async () => {
          if (!act.title.trim() || !act.issuer.trim()) throw new Error("Заповніть назву і ким прийнято");
          if (text.trim().length < 50) throw new Error("Текст акта закороткий");
          const r = await regulationsApi.ingestAct(act, text);
          toast(`Акт проіндексовано: ${r.chunks} фрагментів, редакція індексу ${r.index_revision}`);
          setAct(EMPTY_ACT);
          setText("");
        }}>Проіндексувати акт</ActionButton>
      </div>
    </details>
  );
}

function EditStatus({ hit, onClose }: { hit: SearchHit; onClose: () => void }) {
  const toast = useToast();
  const [act, setAct] = useState<ActIn>({ title: hit.source_title, level: hit.level, issuer: "", status: hit.status });
  return (
    <Modal open title="Статус і строки чинності акта" onClose={onClose}>
      <div className="space-y-3">
        <ActFields act={act} set={setAct} />
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Скасувати</Button>
          <ActionButton variant="primary" onAction={async () => {
            if (!act.issuer.trim()) throw new Error("Вкажіть, ким прийнято акт");
            await regulationsApi.updateStatus(hit.source_id, act);
            toast("Статус акта оновлено");
            onClose();
          }}>Зберегти</ActionButton>
        </div>
      </div>
    </Modal>
  );
}
