/** Огляд: що чекає на мене (візи, перевірка ШІ), мої документи, що потребують уваги, і останні зміни. */
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { documentsApi } from "../../api/endpoints";
import { useApp } from "../../app/context";
import { useTitle } from "../../app/Layout";
import { canUpload } from "../../domain/access";
import { fmtDateTime } from "../../domain/format";
import { Confidence, Status } from "../../components/status";
import { Card, Empty, ErrorBox, Loading, PageHeader, Pill } from "../../components/ui";

export function DashboardPage() {
  useTitle("Огляд");
  const { me, dict } = useApp();
  const inbox = useQuery({ queryKey: ["inbox"], queryFn: documentsApi.inbox, enabled: dict.modules.routing });
  const review = useQuery({ queryKey: ["review-queue"], queryFn: documentsApi.reviewQueue, enabled: me.roles.length > 0 });
  const revision = useQuery({
    queryKey: ["documents", { mine: true, status: "revision_required" }],
    queryFn: () => documentsApi.list({ mine: true, status: "revision_required", limit: 5 }),
  });
  const recent = useQuery({ queryKey: ["documents", { recent: true }], queryFn: () => documentsApi.list({ limit: 8, sort: "-updated_at" }) });
  const overdue = (inbox.data ?? []).filter((x) => x.is_overdue).length;

  return (
    <>
      <PageHeader
        title={`Вітаємо, ${me.display_name.split(" ")[0]}`}
        lead="Документи, що чекають вашого рішення, і результати ШІ, які потрібно перевірити."
        actions={dict.modules.ingest && canUpload(me) ? <Link to="/upload" className="rounded-md bg-brand-700 px-4 py-2 text-sm font-medium text-white no-underline hover:bg-brand-800 hover:no-underline">+ Зареєструвати документ</Link> : null}
      />
      <div className="grid gap-4 md:grid-cols-3">
        {dict.modules.routing && (
          <Tile to="/inbox" label="На погодження" value={inbox.data?.length} loading={inbox.isLoading}
            note={overdue ? <Pill tone="danger">{overdue} прострочено</Pill> : "візи та підписи КЕП"} />
        )}
        <Tile to="/review" label="Перевірка ШІ" value={review.data?.length} loading={review.isLoading} note="впевненість нижче порогу" />
        <Tile to="/documents?mine=1&status=revision_required" label="Повернуто на доопрацювання" value={revision.data?.total} loading={revision.isLoading} note="мої документи" />
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        {dict.modules.routing && (
          <Card aria-labelledby="dash-inbox">
            <h2 id="dash-inbox" className="mb-3 text-lg font-semibold">Чекають мого рішення</h2>
            {inbox.isLoading ? <Loading /> : inbox.error ? <ErrorBox error={inbox.error} /> : inbox.data!.length === 0 ? (
              <Empty>Немає документів на погодження.</Empty>
            ) : (
              <ul className="divide-y divide-slate-100 dark:divide-slate-800">
                {inbox.data!.slice(0, 6).map((x) => (
                  <li key={x.step_id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                    <div>
                      <Link to={`/documents/${x.document_id}/route`} className="font-medium">{x.document_title}</Link>
                      <div className="text-xs text-slate-600 dark:text-slate-400">{x.document_type_name} · {x.author_name}</div>
                    </div>
                    <div className="flex items-center gap-2">
                      {x.requires_kep && <Pill tone="accent">КЕП</Pill>}
                      {x.is_overdue && <Pill tone="danger">прострочено</Pill>}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        )}
        <Card aria-labelledby="dash-recent">
          <h2 id="dash-recent" className="mb-3 text-lg font-semibold">Останні зміни</h2>
          {recent.isLoading ? <Loading /> : recent.error ? <ErrorBox error={recent.error} /> : recent.data!.items.length === 0 ? (
            <Empty>Документів поки немає.</Empty>
          ) : (
            <ul className="divide-y divide-slate-100 dark:divide-slate-800">
              {recent.data!.items.map((d) => (
                <li key={d.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                  <div className="min-w-0">
                    <Link to={`/documents/${d.id}`} className="font-medium">{d.title}</Link>
                    <div className="text-xs text-slate-600 dark:text-slate-400">{d.document_type_name} · {fmtDateTime(d.updated_at)}</div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Status e="DocumentStatus" v={d.status} />
                    {d.needs_review && <Confidence value={d.ai_confidence} />}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}

function Tile({ to, label, value, note, loading }: { to: string; label: string; value: number | undefined; note: React.ReactNode; loading: boolean }) {
  return (
    <Link to={to} className="block rounded-lg border border-slate-200 bg-white p-4 text-slate-900 no-underline shadow-sm hover:border-brand-600 hover:no-underline dark:border-slate-800 dark:bg-slate-900 dark:text-slate-100">
      <div className="text-sm text-slate-600 dark:text-slate-400">{label}</div>
      <div className="mt-1 text-3xl font-semibold tabular-nums">{loading ? "…" : value ?? "—"}</div>
      <div className="mt-1 text-xs text-slate-600 dark:text-slate-400">{note}</div>
    </Link>
  );
}
