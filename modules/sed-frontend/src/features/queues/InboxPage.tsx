/** Черга погодження: активні кроки, призначені мені (візи, погодження, підписи КЕП). */
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { documentsApi } from "../../api/endpoints";
import { useApp } from "../../app/context";
import { useTitle } from "../../app/Layout";
import { fmtDateTime } from "../../domain/format";
import { Empty, ErrorBox, Loading, PageHeader, Pill, Table, Td } from "../../components/ui";

export function InboxPage() {
  useTitle("На погодження");
  const { L } = useApp();
  const navigate = useNavigate();
  const q = useQuery({ queryKey: ["inbox"], queryFn: documentsApi.inbox });
  const items = [...(q.data ?? [])].sort((a, b) => Number(b.is_overdue) - Number(a.is_overdue));
  return (
    <>
      <PageHeader title="На погодження" lead="Документи, що чекають вашої візи або підпису. Наступний учасник маршруту отримає документ лише після вашого рішення." />
      {q.isLoading ? <Loading /> : q.error ? <ErrorBox error={q.error} /> : !items.length ? (
        <Empty>Немає документів, що чекають вашого рішення.</Empty>
      ) : (
        <Table caption="Документи на погодження" head={["Документ", "Підрозділ / автор", "Що потрібно", "Отримано", "Строк"]}>
          {items.map((x) => (
            <tr key={x.step_id} className="cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800/50" onClick={(e) => { if (!(e.target as HTMLElement).closest("a")) navigate(`/documents/${x.document_id}/route`); }}>
              <Td>
                <Link to={`/documents/${x.document_id}/route`} className="font-medium">{x.document_title}</Link>
                <div className="text-xs text-slate-500">{x.document_type_name}</div>
                {x.delegated_from && <div className="text-xs">замість {x.delegated_from}</div>}
              </Td>
              <Td className="text-xs">{x.org_unit_name}<div className="text-slate-500">{x.author_name}</div></Td>
              <Td>
                <div className="flex flex-wrap gap-1"><Pill tone="accent">{L("StepKind", x.step_kind)}</Pill>{x.requires_kep && <Pill>КЕП</Pill>}</div>
                {x.acting_role && <div className="mt-1 text-xs text-slate-500">як {L("UserRole", x.acting_role).toLowerCase()}</div>}
              </Td>
              <Td className="whitespace-nowrap text-xs">{fmtDateTime(x.activated_at)}</Td>
              <Td className="whitespace-nowrap text-xs">{x.due_at ? fmtDateTime(x.due_at) : "—"}{x.is_overdue && <div className="mt-1"><Pill tone="danger">прострочено</Pill></div>}</Td>
            </tr>
          ))}
        </Table>
      )}
    </>
  );
}
