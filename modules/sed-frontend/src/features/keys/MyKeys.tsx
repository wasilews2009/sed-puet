import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "../../api/client";
import { kepApi } from "../../api/endpoints";
import type { KeyBinding } from "../../api/types";
import { useApp } from "../../app/context";
import { fmtDate, fmtDateTime, shortHash } from "../../domain/format";
import { useAskReason } from "../../components/dialog";
import { useToast } from "../../components/toast";
import { ActionButton, Button, Checkbox, Empty, ErrorBox, Loading, Mono, Notice, Pill, Table, Td } from "../../components/ui";

export function MyKeys({ onBind }: { onBind: () => void }) {
  const { L } = useApp();
  const [all, setAll] = useState(false);
  const qc = useQueryClient();
  const ask = useAskReason();
  const toast = useToast();
  const q = useQuery({ queryKey: ["kep-keys", all], queryFn: () => kepApi.myKeys(all) });

  if (q.isLoading) return <Loading />;
  if (q.error) {
    if (q.error instanceof ApiError && q.error.status === 503)
      return <Notice tone="warn">Модуль ключів підпису не підключено на цьому стенді.</Notice>;
    return <ErrorBox error={q.error} />;
  }
  const keys = q.data ?? [];
  const active = keys.filter((k) => !k.revoked_at);
  const revoke = async (k: KeyBinding) => {
    const reason = await ask({
      title: `Відкріпити ключ «${k.subject_cn}»`,
      label: "Причина (втрата носія, звільнення, компрометація…). Якщо ключ скомпрометовано — так і вкажіть: внутрішній сертифікат буде відкликано з цією причиною",
      confirmText: "Відкріпити",
    });
    if (!reason) return;
    await kepApi.revoke(k.id, reason);
    toast("Ключ відкріплено: підписи ним більше не приймаються");
    await qc.invalidateQueries({ queryKey: ["kep-keys"] });
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Checkbox label="Показати відкріплені" checked={all} onChange={(e) => setAll(e.target.checked)} />
        <Button variant="primary" onClick={onBind}>+ Закріпити КЕП</Button>
      </div>
      {active.some((k) => k.expires_soon) && <Notice tone="warn">Строк дії одного з ваших сертифікатів скоро спливає. Отримайте новий у свого КНЕДП і закріпіть його.</Notice>}
      {!keys.length ? (
        <Empty>За вами ще не закріплено жодного ключа. Закріпіть КЕП Дії, ПриватБанку чи іншого КНЕДП, щоб накладати візи з юридичною силою.</Empty>
      ) : (
        <Table caption="Мої ключі підпису" head={["Власник сертифіката", "Рівень", "Видавець", "Строк дії", "Закріплено", ""]}>
          {keys.map((k) => (
            <tr key={k.id} className={k.revoked_at ? "opacity-60" : undefined}>
              <Td><b>{k.subject_cn}</b><div><Mono title={k.fingerprint_sha256}>SHA-256 {shortHash(k.fingerprint_sha256, 8)}</Mono></div>
                {k.key_format && <div className="text-xs text-slate-500">{L("KeyFormat", k.key_format)}</div>}</Td>
              <Td><Pill tone={k.level === "qualified" ? "ok" : k.level === "internal" ? "accent" : "warn"}>{k.level_uk}</Pill></Td>
              <Td className="text-xs">{k.issuer}<div><Mono>№ {k.serial}</Mono></div></Td>
              <Td className="whitespace-nowrap text-xs">{fmtDate(k.valid_from)} — {fmtDate(k.valid_to)}{k.expires_soon && !k.revoked_at && <div className="mt-1"><Pill tone="warn">скоро спливає</Pill></div>}</Td>
              <Td className="text-xs">{fmtDateTime(k.bound_at)}<div className="text-slate-500">{L("BoundVia", k.bound_via)}</div>
                {k.revoked_at && <div className="mt-1"><Pill tone="danger">відкріплено {fmtDate(k.revoked_at)}</Pill>{k.revoke_reason && <div>{k.revoke_reason}</div>}</div>}</Td>
              <Td>{!k.revoked_at && <ActionButton size="sm" variant="danger" onAction={() => revoke(k)}>Відкріпити</ActionButton>}</Td>
            </tr>
          ))}
        </Table>
      )}
    </div>
  );
}
