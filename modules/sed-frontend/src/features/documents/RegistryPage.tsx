/** Реєстр документів: пошук, фільтри (у URL — можна поділитись посиланням), сторінки. Сервер повертає лише доступне. */
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { documentsApi, type DocumentFilter } from "../../api/endpoints";
import type { DocumentSort, DocumentStatus } from "../../api/types";
import { useApp } from "../../app/context";
import { useTitle } from "../../app/Layout";
import { canUpload } from "../../domain/access";
import { fmtDate, fmtDateTime } from "../../domain/format";
import { Confidence, Status } from "../../components/status";
import { Button, Checkbox, Empty, ErrorBox, Input, Loading, PageHeader, Pill, Select, Table, Td } from "../../components/ui";

const PAGE = 25;

export function RegistryPage() {
  useTitle("Документи");
  const { dict, me, L } = useApp();
  const [sp, setSp] = useSearchParams();
  const navigate = useNavigate();
  const f: DocumentFilter = {
    q: sp.get("q") ?? "",
    status: (sp.get("status") ?? "") as DocumentStatus | "",
    type_code: sp.get("type") ?? "",
    org_unit_id: sp.get("unit") ?? "",
    mine: sp.get("mine") === "1",
    needs_review: sp.get("review") === "1" || undefined,
    sort: (sp.get("sort") as DocumentSort) || "-updated_at",
    limit: PAGE,
    offset: Number(sp.get("offset") ?? 0),
  };
  const [q, setQ] = useState(f.q ?? "");
  useEffect(() => {
    const t = setTimeout(() => {
      if ((sp.get("q") ?? "") !== q) set({ q: q.trim() });
    }, 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  const set = (patch: Record<string, string | boolean>) => {
    const next = new URLSearchParams(sp);
    for (const [k, v] of Object.entries(patch)) {
      if (v === "" || v === false) next.delete(k);
      else next.set(k, v === true ? "1" : v);
    }
    if (!("offset" in patch)) next.delete("offset");
    setSp(next, { replace: true });
  };

  const page = useQuery({ queryKey: ["documents", f], queryFn: () => documentsApi.list(f), placeholderData: keepPreviousData });

  return (
    <>
      <PageHeader
        title="Документи"
        lead="Реєстр документів, доступних вам за роллю і підрозділом."
        actions={dict.modules.ingest && canUpload(me) ? <Link to="/upload" className="rounded-md bg-brand-700 px-4 py-2 text-sm font-medium text-white no-underline hover:bg-brand-800 hover:no-underline">+ Зареєструвати</Link> : null}
      />
      <form role="search" aria-label="Пошук і фільтри документів" className="mb-4 grid gap-3 rounded-lg border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900 md:grid-cols-6" onSubmit={(e) => e.preventDefault()}>
        <Input type="search" className="md:col-span-2" placeholder="Заголовок, індекс, зміст…" aria-label="Пошук" value={q} onChange={(e) => setQ(e.target.value)} />
        <Select aria-label="Статус" value={f.status} onChange={(e) => set({ status: e.target.value })}>
          <option value="">Усі статуси</option>
          {Object.entries((dict.labels as Record<string, Record<string, string>>).DocumentStatus ?? {}).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </Select>
        <Select aria-label="Вид документа" value={f.type_code} onChange={(e) => set({ type: e.target.value })}>
          <option value="">Усі види</option>
          {dict.document_types.map((t) => <option key={t.code} value={t.code}>{t.name}</option>)}
        </Select>
        <Select aria-label="Підрозділ" value={f.org_unit_id} onChange={(e) => set({ unit: e.target.value })}>
          <option value="">Усі підрозділи</option>
          {dict.org_units.map((u) => <option key={u.id} value={u.id}>{u.short_name || u.name}</option>)}
        </Select>
        <Select aria-label="Сортування" value={f.sort} onChange={(e) => set({ sort: e.target.value === "-updated_at" ? "" : e.target.value })}>
          <option value="-updated_at">Спершу змінені</option>
          <option value="-created_at">Спершу нові</option>
          <option value="created_at">Спершу старі</option>
          <option value="title">За заголовком</option>
        </Select>
        <div className="flex flex-wrap gap-4 md:col-span-6">
          <Checkbox label="Лише мої" checked={!!f.mine} onChange={(e) => set({ mine: e.target.checked })} />
          <Checkbox label="Потребують перевірки ШІ" checked={!!f.needs_review} onChange={(e) => set({ review: e.target.checked })} />
        </div>
      </form>

      {page.isLoading ? <Loading /> : page.error ? <ErrorBox error={page.error} /> : page.data!.items.length === 0 ? (
        <Empty>Документів не знайдено. Змініть фільтри або зареєструйте новий документ.</Empty>
      ) : (
        <>
          <Table caption="Документи" head={["Заголовок", "Вид", "Статус", "Підрозділ / автор", "Реєстрація", "ШІ: реквізити", "Комплаєнс", "Змінено"]}>
            {page.data!.items.map((d) => (
              <tr key={d.id} className="cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800/50" onClick={(e) => { if (!(e.target as HTMLElement).closest("a")) navigate(`/documents/${d.id}`); }}>
                <Td>
                  <Link to={`/documents/${d.id}`} className="font-medium">{d.title}</Link>
                  {d.access_level !== "internal" && <div className="mt-1"><Status e="AccessLevel" v={d.access_level} /></div>}
                </Td>
                <Td className="text-xs">{d.document_type_name}<div className="text-slate-500">{L("DocumentDirection", d.direction)}</div></Td>
                <Td><Status e="DocumentStatus" v={d.status} /></Td>
                <Td className="text-xs">{d.org_unit_name}<div className="text-slate-500">{d.author_name}</div></Td>
                <Td className="whitespace-nowrap text-xs">{d.registration_number ? `№ ${d.registration_number}` : "—"}{d.registration_date && <div className="text-slate-500">{fmtDate(d.registration_date)}</div>}</Td>
                <Td><Confidence value={d.ai_confidence} />{d.needs_review && <div className="mt-1"><Pill tone="warn">чекає перевірки</Pill></div>}</Td>
                <Td>{d.compliance_verdict ? <Status e="ComplianceVerdict" v={d.compliance_verdict} /> : <span className="text-xs text-slate-500">не перевірявся</span>}
                  {d.open_findings > 0 && <div className="mt-1"><Pill tone="danger">{d.open_findings} відкр. зауваж.</Pill></div>}</Td>
                <Td className="whitespace-nowrap text-xs text-slate-500">{fmtDateTime(d.updated_at)}</Td>
              </tr>
            ))}
          </Table>
          <nav aria-label="Сторінки" className="mt-3 flex flex-wrap items-center justify-between gap-2 text-sm text-slate-600 dark:text-slate-400">
            <span aria-live="polite">Показано {page.data!.offset + 1}–{page.data!.offset + page.data!.items.length} з {page.data!.total}</span>
            <span className="flex gap-2">
              <Button size="sm" disabled={page.data!.offset === 0} onClick={() => set({ offset: String(Math.max(0, page.data!.offset - PAGE)) })}>← Попередні</Button>
              <Button size="sm" disabled={page.data!.offset + page.data!.items.length >= page.data!.total} onClick={() => set({ offset: String(page.data!.offset + PAGE) })}>Наступні →</Button>
            </span>
          </nav>
        </>
      )}
    </>
  );
}
