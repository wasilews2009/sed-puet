/** Черга ручної валідації: результати ШІ з впевненістю нижче порогу (Human-in-the-loop). */
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { documentsApi } from "../../api/endpoints";
import { useApp } from "../../app/context";
import { useTitle } from "../../app/Layout";
import { fmtDateTime } from "../../domain/format";
import { Confidence } from "../../components/status";
import { Empty, ErrorBox, Loading, PageHeader, Pill, Table, Td } from "../../components/ui";

export function ReviewQueuePage() {
  useTitle("Перевірка ШІ");
  const { threshold } = useApp();
  const navigate = useNavigate();
  const q = useQuery({ queryKey: ["review-queue"], queryFn: documentsApi.reviewQueue });
  return (
    <>
      <PageHeader title="Перевірка результатів ШІ"
        lead={`Реквізити і звіти нормативного аудиту з впевненістю нижче ${Math.round(threshold * 100)}% не потрапляють у картку, доки людина їх не підтвердить або не виправить.`} />
      {q.isLoading ? <Loading /> : q.error ? <ErrorBox error={q.error} /> : !q.data!.length ? (
        <Empty>Черга порожня: усі результати ШІ у ваших документах перевірено.</Empty>
      ) : (
        <Table caption="Результати ШІ, що чекають перевірки" head={["Документ", "Підрозділ", "Що перевірити", "Впевненість", "Надійшло"]}>
          {q.data!.map((x) => {
            const tab = x.task === "requisites" ? "requisites" : "compliance";
            return (
              <tr key={x.ai_run_id} className="cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800/50" onClick={(e) => { if (!(e.target as HTMLElement).closest("a")) navigate(`/documents/${x.document_id}/${tab}`); }}>
                <Td><Link to={`/documents/${x.document_id}/${tab}`} className="font-medium">{x.document_title}</Link></Td>
                <Td className="text-xs">{x.org_unit_name}</Td>
                <Td>{x.task === "requisites" ? (
                  <><Pill tone="warn">Реквізити</Pill> <span className="text-xs text-slate-500">{x.pending_fields ? `${x.pending_fields} знач.` : "ввести вручну"}</span></>
                ) : <Pill tone="warn">Звіт нормативного аудиту</Pill>}</Td>
                <Td><Confidence value={x.confidence} /></Td>
                <Td className="whitespace-nowrap text-xs">{fmtDateTime(x.queued_at)}</Td>
              </tr>
            );
          })}
        </Table>
      )}
    </>
  );
}
