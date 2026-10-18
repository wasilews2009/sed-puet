/**
 * Погодження: панель мого рішення (віза / КЕП / відхилення), хід маршруту, запуск за типовим шаблоном.
 * Наступний учасник отримує документ лише після валідного підпису попереднього (перевіряє сервер).
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "../../api/client";
import { routingApi } from "../../api/endpoints";
import type { DocumentCard, RoutePlan, StepOut } from "../../api/types";
import { useApp } from "../../app/context";
import { fmtDateTime } from "../../domain/format";
import { SignaturePicker } from "../../kep/SignaturePicker";
import { useAskReason } from "../../components/dialog";
import { Status } from "../../components/status";
import { ActionButton, Button, Checkbox, ErrorBox, Field, Loading, Notice, Pill, Textarea, cx } from "../../components/ui";
import { useCardAction } from "./useCard";

export function RouteTab({ card }: { card: DocumentCard }) {
  const { L, me } = useApp();
  const { run } = useCardAction(card);
  const ask = useAskReason();
  const r = card.route;
  // назви етапів (title_uk) є лише у поданні модуля маршрутів — див. README, «Потрібні зміни API»
  const view = useQuery({ queryKey: ["route", card.id, r?.id], queryFn: () => routingApi.documentRoute(card.id), enabled: !!r });
  const titles = new Map((view.data?.steps ?? []).map((s) => [s.id, s.title_uk]));
  const myStep = r?.steps.find((s) => s.id === card.permissions.my_active_step_id);
  const canCancel = r?.status === "active" && (card.author.id === me.id || r.initiated_by.id === me.id);

  return (
    <div className="space-y-5">
      {myStep && <DecisionPanel card={card} step={myStep} title={titles.get(myStep.id)} />}
      {r ? (
        <section aria-labelledby="route-h" className="space-y-3">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <h3 id="route-h" className="font-semibold">{r.template_name || "Маршрут погодження"}</h3>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-600 dark:text-slate-400">
                <Status e="RouteStatus" v={r.status} />
                <span>ініціатор: {r.initiated_by.name}</span>
                {r.started_at && <span>· запущено {fmtDateTime(r.started_at)}</span>}
                {r.completed_at && <span>· завершено {fmtDateTime(r.completed_at)}</span>}
              </div>
            </div>
            {canCancel && (
              <ActionButton size="sm" variant="danger" onAction={async () => {
                const reason = await ask({ title: "Скасувати маршрут", label: "Причина скасування", confirmText: "Скасувати маршрут" });
                if (reason) await run(() => routingApi.cancel(r.id, reason), "Маршрут скасовано");
              }}>Скасувати маршрут</ActionButton>
            )}
          </div>
          {r.cancel_reason && <Notice tone="warn">Скасовано: {r.cancel_reason}</Notice>}
          {(view.data?.warnings ?? []).map((w) => <Notice key={w} tone="warn">{w}</Notice>)}
          <ol className="space-y-3">
            {r.steps.map((s) => <StepItem key={s.id} card={card} s={s} title={titles.get(s.id)} L={L} />)}
          </ol>
        </section>
      ) : (
        <p className="text-sm text-slate-600 dark:text-slate-400">Маршрут погодження ще не сформовано.</p>
      )}
      {card.permissions.start_route && (!r || r.status !== "active") && <StartRoute card={card} />}
    </div>
  );
}

function StepItem({ card, s, title, L }: { card: DocumentCard; s: StepOut; title?: string; L: (e: string, v: string | null | undefined) => string }) {
  const sig = card.signatures.find((x) => x.approval_step_id === s.id);
  const dot = { active: "bg-sky-500", approved: "bg-emerald-600", approved_with_remarks: "bg-amber-500", rejected: "bg-red-600", expired: "bg-red-600" }[s.status as string] ?? "bg-slate-300";
  return (
    <li className={cx("relative rounded-md border p-3 pl-8 dark:border-slate-700", s.status === "active" ? "border-sky-300 bg-sky-50/50 dark:bg-sky-950/30" : "border-slate-200")}>
      <span aria-hidden="true" className={cx("absolute left-3 top-4 h-3 w-3 rounded-full", dot)} />
      <div className="flex flex-wrap items-center gap-2">
        <b>{s.assignee.name}</b>
        {s.acting_role && <span className="text-xs text-slate-600 dark:text-slate-400">{L("UserRole", s.acting_role)}</span>}
        <Status e="StepStatus" v={s.status} />
        {s.requires_kep && <Pill tone="accent">КЕП</Pill>}
        {s.is_overdue && <Pill tone="danger">прострочено</Pill>}
        {!s.is_mandatory && <Pill>необов'язковий</Pill>}
      </div>
      {title && <div className="text-sm">{title}</div>}
      <div className="text-xs text-slate-600 dark:text-slate-400">
        Етап {s.stage_no} · {L("StepKind", s.step_kind)}
        {s.original_assignee && ` · замість ${s.original_assignee.name}`}
        {s.activated_at && ` · отримано ${fmtDateTime(s.activated_at)}`}
        {s.due_at && ` · строк ${fmtDateTime(s.due_at)}`}
        {s.decided_at && ` · рішення ${fmtDateTime(s.decided_at)}`}
      </div>
      {s.decision_comment && <blockquote className="mt-1 text-sm">{s.decision_comment}</blockquote>}
      {sig && <div className="mt-1 text-xs">КЕП: {sig.cert_subject_cn} · <Status e="SignatureValidation" v={sig.validation_status} /> · {fmtDateTime(sig.signed_at)}</div>}
    </li>
  );
}

function StartRoute({ card }: { card: DocumentCard }) {
  const { run } = useCardAction(card);
  const [plan, setPlan] = useState<RoutePlan | null>(null);
  const [ack, setAck] = useState(false);
  const critical = (card.compliance?.findings ?? []).filter((f) => f.status === "open" && f.severity === "critical").length;
  return (
    <section aria-labelledby="start-h" className="space-y-3 rounded-md border border-slate-200 p-4 dark:border-slate-700">
      <h3 id="start-h" className="font-semibold">{card.route ? "Повторне погодження" : "Погодження документа"}</h3>
      <p className="text-xs text-slate-600 dark:text-slate-400">Учасників визначає типовий маршрут для виду документа і підрозділу: наступний учасник отримує документ лише після позитивної візи попереднього.</p>
      {!plan ? (
        <ActionButton onAction={async () => setPlan(await routingApi.preview(card.id))}>Сформувати маршрут</ActionButton>
      ) : (
        <div className="space-y-3">
          <b>{plan.template_name}</b>
          <ol className="ml-5 list-decimal space-y-1 text-sm">
            {plan.steps.map((s) => (
              <li key={`${s.stage_no}-${s.ordinal}`} className={s.will_skip ? "text-slate-500 line-through" : undefined}>
                {s.title_uk}: <b>{s.assignee_name}</b>{s.requires_kep && " (КЕП)"}
                {s.deadline_hours ? <span className="text-xs text-slate-500"> · строк {s.deadline_hours} год</span> : null}
                {s.will_skip && <span className="no-underline"> — буде пропущено: {s.skip_reason}</span>}
              </li>
            ))}
          </ol>
          {critical > 0 && (
            <Notice tone="danger">
              Відкритих критичних зауважень нормативного аудиту: {critical}. Маршрут не запуститься без вашого свідомого підтвердження.
              <div className="mt-2"><Checkbox label="Запустити попри відкриті критичні зауваження (рішення фіксується в журналі)" checked={ack} onChange={(e) => setAck(e.target.checked)} /></div>
            </Notice>
          )}
          <div className="flex gap-2">
            <ActionButton variant="primary" onAction={() => run(() => routingApi.create(card.id, ack), "Маршрут запущено: документ надіслано першому учаснику")}>Запустити погодження</ActionButton>
            <Button onClick={() => setPlan(null)}>Скасувати</Button>
          </div>
        </div>
      )}
    </section>
  );
}

function DecisionPanel({ card, step, title }: { card: DocumentCard; step: StepOut; title?: string }) {
  const { L } = useApp();
  const { run } = useCardAction(card);
  const [comment, setComment] = useState("");
  const decision = comment.trim() ? "approved_with_remarks" : "approved";
  const req = useQuery({ queryKey: ["signing-request", step.id], queryFn: () => routingApi.signingRequest(step.id), enabled: step.requires_kep, staleTime: 0 });

  return (
    <section aria-labelledby="decide-h" className="space-y-3 rounded-lg border-2 border-sky-400 p-4">
      <h3 id="decide-h" className="text-lg font-semibold">Ваше рішення: {L("StepKind", step.step_kind).toLowerCase()}{title ? ` — ${title}` : ""}</h3>
      <p className="text-sm text-slate-600 dark:text-slate-400">Перегляньте оригінал, реквізити і звіт нормативного аудиту. Рішення приймаєте ви: ШІ лише підказує.</p>
      <Field label="Зауваження" hint="Обов'язкові для відхилення; з ними віза матиме статус «із зауваженнями»">
        {(p) => <Textarea {...p} rows={3} value={comment} onChange={(e) => setComment(e.target.value)} />}
      </Field>
      {step.requires_kep ? (
        req.isLoading ? <Loading /> : req.error ? (
          <ErrorBox error={req.error instanceof ApiError ? req.error : new Error("Не вдалося отримати дані для підпису")} />
        ) : req.data && (
          <>
            {(req.data.accepted_levels_uk ?? []).length > 0 && <p className="text-xs text-slate-600 dark:text-slate-400">Сервер прийме: {(req.data.accepted_levels_uk ?? []).join(", ")}. Формат: {req.data.signature_container}.</p>}
            <SignaturePicker
              target={{ contentSha256: req.data.content_sha256, description: `${req.data.file_name} (версія документа)`, acceptedLevels: req.data.accepted_levels ?? [], stepId: step.id }}
              actionLabel={decision === "approved" ? "Погодити і підписати" : "Погодити із зауваженнями і підписати"}
              onSigned={(s) => run(() => routingApi.sign(step.id, { signature_base64: s.signatureBase64, provider: s.provider, decision, comment: comment.trim() || null }),
                "Візу з КЕП накладено. Документ передано наступному учаснику")}
            />
          </>
        )
      ) : (
        <ActionButton variant="ok" onAction={() => run(() => routingApi.decide(step.id, decision, comment.trim() || null), "Рішення збережено")}>
          {decision === "approved" ? "Погодити" : "Погодити із зауваженнями"}
        </ActionButton>
      )}
      <div className="border-t border-slate-200 pt-3 dark:border-slate-700">
        <ActionButton variant="danger" onAction={() => {
          if (!comment.trim()) throw new Error("Вкажіть причину відхилення в полі зауважень");
          return run(() => routingApi.decide(step.id, "rejected", comment.trim()), "Документ повернуто на доопрацювання");
        }}>Відхилити (повернути на доопрацювання)</ActionButton>
      </div>
    </section>
  );
}
