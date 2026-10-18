import type { DocumentCard } from "../../api/types";
import { useApp } from "../../app/context";
import { fmtDateTime } from "../../domain/format";
import { Status } from "../../components/status";

export function HistoryTab({ card }: { card: DocumentCard }) {
  const { L } = useApp();
  if (!card.history.length) return <p className="text-sm text-slate-600">Змін статусу ще не було.</p>;
  return (
    <ol className="relative space-y-4 border-l-2 border-slate-200 pl-5 dark:border-slate-700">
      {card.history.map((x, i) => (
        <li key={i}>
          <span aria-hidden="true" className="absolute -left-[7px] mt-1.5 h-3 w-3 rounded-full bg-brand-600" />
          <div className="flex flex-wrap items-center gap-2">
            {x.from_status && <><Status e="DocumentStatus" v={x.from_status} /><span aria-label="змінено на">→</span></>}
            <Status e="DocumentStatus" v={x.to_status} />
          </div>
          <div className="mt-1 text-xs text-slate-600 dark:text-slate-400">
            {fmtDateTime(x.changed_at)} · {x.changed_by ? x.changed_by.name : L("ActorType", x.actor_type)}
            {x.reason ? ` · ${x.reason}` : ""}
          </div>
        </li>
      ))}
    </ol>
  );
}
