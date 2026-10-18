/**
 * Картка документа: шапка з реквізитами реєстрації, нагадування про роль ШІ, вкладки
 * (реквізити ШІ з ручною валідацією, нормативний аудит, погодження з КЕП, версії і підписи, історія)
 * і перегляд оригіналу поруч.
 */
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../../api/client";
import type { DocumentCard, VersionOut } from "../../api/types";
import { useApp } from "../../app/context";
import { useTitle } from "../../app/Layout";
import { fmtDate, fmtDateTime } from "../../domain/format";
import { Status } from "../../components/status";
import { TabPanel, Tabs, type TabDef } from "../../components/tabs";
import { Button, Card, ErrorBox, KV, Loading, Notice, Pill } from "../../components/ui";
import { ComplianceTab } from "./ComplianceTab";
import { FilePreview } from "./FilePreview";
import { HistoryTab } from "./HistoryTab";
import { RequisitesTab } from "./RequisitesTab";
import { RouteTab } from "./RouteTab";
import { useCard } from "./useCard";
import { VersionsTab } from "./VersionsTab";

const TAB_KEYS = ["requisites", "compliance", "route", "versions", "history"] as const;
type TabKey = (typeof TAB_KEYS)[number];

export function CardPage() {
  const { id = "", tab } = useParams();
  const q = useCard(id);
  useTitle(q.data?.title ?? "Картка документа");
  if (q.isLoading) return <Loading />;
  if (q.error) {
    if (q.error instanceof ApiError && q.error.status === 404)
      return (
        <Notice tone="warn" title="Документ не знайдено">
          Документа не існує або у вас немає до нього доступу. <Link to="/documents">До реєстру</Link>
        </Notice>
      );
    return <ErrorBox error={q.error} />;
  }
  return <CardView card={q.data!} tab={tab} />;
}

function CardView({ card, tab }: { card: DocumentCard; tab?: string }) {
  const navigate = useNavigate();
  const { dict, L } = useApp();
  const fallback: TabKey = card.permissions.my_active_step_id ? "route" : "requisites";
  const current: TabKey = (TAB_KEYS as readonly string[]).includes(tab ?? "") ? (tab as TabKey) : fallback;
  const [preview, setPreview] = useState<VersionOut | null>(null);
  const pending = (card.extraction?.fields ?? []).filter((f) => f.review_status === "pending").length;
  const openFindings = (card.compliance?.findings ?? []).filter((f) => f.status === "open").length;

  const tabs: TabDef[] = [
    { key: "requisites", label: "Реквізити (ШІ)", badge: pending },
    { key: "compliance", label: "Нормативний аудит", badge: openFindings },
    { key: "route", label: "Погодження", badge: card.permissions.my_active_step_id ? 1 : 0 },
    { key: "versions", label: "Версії і підписи" },
    { key: "history", label: "Історія" },
  ];
  const currentVersion = card.versions.find((v) => v.id === card.current_version_id) ?? null;

  return (
    <div className="space-y-4">
      <Card className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <Status e="DocumentStatus" v={card.status} />
              <Pill tone="accent">{card.document_type.name}</Pill>
              <Status e="AccessLevel" v={card.access_level} />
              {card.document_type.requires_kep && <Pill>потребує КЕП</Pill>}
            </div>
            <h1 className="mt-2 text-2xl font-semibold">{card.title}</h1>
            {card.summary && <p className="mt-1 max-w-4xl text-sm text-slate-600 dark:text-slate-400">{card.summary}</p>}
          </div>
          <div className="flex flex-wrap gap-2">
            {currentVersion && <Button onClick={() => setPreview(currentVersion)}>Переглянути оригінал</Button>}
            <Link to="/documents" className="rounded-md border border-slate-300 px-4 py-2 text-sm no-underline hover:bg-slate-100 dark:border-slate-600 dark:hover:bg-slate-800">← До реєстру</Link>
          </div>
        </div>
        <KV
          items={[
            ["Підрозділ", card.org_unit.name],
            ["Автор (зареєстрував)", card.author.name],
            ["Напрям", L("DocumentDirection", card.direction)],
            ["Реєстраційний індекс", card.registration_number ? `№ ${card.registration_number} від ${fmtDate(card.registration_date)}` : "не зареєстровано"],
            ...(card.correspondent_name ? ([["Кореспондент", `${card.correspondent_name}${card.correspondent_number ? `, № ${card.correspondent_number}` : ""}${card.correspondent_date ? ` від ${fmtDate(card.correspondent_date)}` : ""}`]] as [string, string][]) : []),
            ...(card.due_date ? ([["Контрольний строк", fmtDate(card.due_date)]] as [string, string][]) : []),
            ["Створено", fmtDateTime(card.created_at)],
            ["Змінено", fmtDateTime(card.updated_at)],
          ]}
        />
      </Card>
      <Notice tone="accent">{card.disclaimer}</Notice>
      <div className={preview ? "grid gap-4 xl:grid-cols-2" : ""}>
        <Card>
          <Tabs idBase="card" tabs={tabs} current={current} onChange={(k) => navigate(`/documents/${card.id}/${k}`, { replace: true })} />
          <TabPanel idBase="card" current={current}>
            {current === "requisites" && <RequisitesTab card={card} />}
            {current === "compliance" && (dict.modules.compliance ? <ComplianceTab card={card} /> : <Notice>Модуль нормативного аудиту не підключено на цьому стенді (потрібні Qdrant, сервер ембедингів і LLM).</Notice>)}
            {current === "route" && (dict.modules.routing ? <RouteTab card={card} /> : <Notice>Модуль маршрутів погодження не підключено на цьому стенді.</Notice>)}
            {current === "versions" && <VersionsTab card={card} onPreview={setPreview} />}
            {current === "history" && <HistoryTab card={card} />}
          </TabPanel>
        </Card>
        {preview && <FilePreview card={card} version={preview} onClose={() => setPreview(null)} />}
      </div>
    </div>
  );
}
