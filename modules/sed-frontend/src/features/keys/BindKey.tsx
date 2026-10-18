/**
 * Закріплення КЕП за обліковим записом (доказ володіння ключем):
 * 1) сервер видає одноразовий виклик (дані з вашим ідентифікатором і строком дії);
 * 2) ви підписуєте його своїм ключем на своєму комп'ютері (ІІТ «Користувач ЦСК», Дія.Підпис, або PKCS#12 у браузері);
 * 3) сервер перевіряє підпис, ланцюжок до КНЕДП і відкликання — і закріплює сертифікат.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { kepApi } from "../../api/endpoints";
import type { Challenge, KeyFormat } from "../../api/types";
import { fmtDateTime } from "../../domain/format";
import { fallbackLabels } from "../../domain/labels";
import { fromBase64 } from "../../kep/encoding";
import { SignaturePicker } from "../../kep/SignaturePicker";
import { useToast } from "../../components/toast";
import { ActionButton, Field, Mono, Notice, Select } from "../../components/ui";
import { downloadBlob } from "./download";

export function BindKey({ onDone, mock }: { onDone: () => void; mock: boolean }) {
  const [ch, setCh] = useState<Challenge | null>(null);
  const [format, setFormat] = useState<KeyFormat | "">("");
  const qc = useQueryClient();
  const toast = useToast();
  const expired = ch ? new Date(ch.expires_at) < new Date() : false;

  return (
    <ol className="space-y-5">
      <li className="space-y-2">
        <h3 className="font-semibold">1. Отримайте виклик</h3>
        <p className="text-sm text-slate-600 dark:text-slate-400">Виклик одноразовий і діє обмежений час. Його підписання доводить, що ключ ваш; сам ключ і пароль сервер не отримує.</p>
        <ActionButton variant={ch ? "secondary" : "primary"} onAction={async () => setCh(await kepApi.challenge())}>{ch ? "Отримати новий виклик" : "Отримати виклик"}</ActionButton>
      </li>
      {ch && (
        <>
          <li className="space-y-2">
            <h3 className="font-semibold">2. Підпишіть виклик своїм ключем</h3>
            {expired && <Notice tone="warn">Строк дії виклику минув. Отримайте новий.</Notice>}
            <Notice>{ch.instructions_uk}</Notice>
            <div className="text-sm">
              <div>Дійсний до: <b>{fmtDateTime(ch.expires_at)}</b></div>
              <div className="mt-1">Дані: <Mono>{ch.data_text}</Mono></div>
            </div>
            <ActionButton size="sm" onAction={() => downloadBlob(new Blob([fromBase64(ch.data_base64)], { type: "application/octet-stream" }), "sed-puet-challenge.txt")}>
              Завантажити файл виклику для підпису
            </ActionButton>
            <Field label="Тип вашого ключа (для довідки)">
              {(p) => (
                <Select {...p} value={format} onChange={(e) => setFormat(e.target.value as KeyFormat | "")}>
                  <option value="">Не вказувати</option>
                  {Object.entries(fallbackLabels("KeyFormat")).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </Select>
              )}
            </Field>
          </li>
          <li className="space-y-2">
            <h3 className="font-semibold">3. Надішліть підпис</h3>
            <SignaturePicker
              target={{ contentSha256: ch.content_sha256, description: "виклик для закріплення ключа", acceptedLevels: mock ? ["qualified", "mock"] : ["qualified"] }}
              actionLabel="Закріпити ключ"
              onSigned={async (s) => {
                const b = await kepApi.bind(ch.challenge_token, s.signatureBase64, format || null);
                toast(`Ключ «${b.subject_cn}» закріплено`);
                await qc.invalidateQueries({ queryKey: ["kep-keys"] });
                onDone();
              }}
            />
            <p className="text-xs text-slate-600 dark:text-slate-400">Закріпити можна лише кваліфікований сертифікат КНЕДП. Внутрішні ключі АІ-СЕД закріплюються автоматично під час видачі.</p>
          </li>
        </>
      )}
    </ol>
  );
}
