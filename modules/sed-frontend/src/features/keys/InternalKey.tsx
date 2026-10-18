/**
 * Внутрішній ключ АІ-СЕД ПУЕТ: для внутрішніх віз, коли КЕП ще немає. НЕ є КЕП і ніколи не приймається
 * на кроці, що надає документу юридичної сили (це перевіряє сервер).
 *  - файл-ключ PKCS#12: генерується сервером і віддається один раз (копії на сервері немає);
 *  - CSR: ключ генерується на вашому комп'ютері (`keytool new-key`), сервер лише підписує сертифікат — надійніше.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "../../api/client";
import { kepApi } from "../../api/endpoints";
import type { InternalCert } from "../../api/types";
import { useApp } from "../../app/context";
import { fmtDate } from "../../domain/format";
import { passwordProblems } from "../../domain/password";
import { useToast } from "../../components/toast";
import { ActionButton, Field, Input, KV, Mono, Notice, Textarea } from "../../components/ui";
import { downloadBlob } from "./download";

export function InternalKey() {
  const { me } = useApp();
  const qc = useQueryClient();
  const toast = useToast();
  const [pwd, setPwd] = useState("");
  const [pwd2, setPwd2] = useState("");
  const [csr, setCsr] = useState("");
  const [issued, setIssued] = useState<InternalCert | null>(null);
  const problems = pwd ? passwordProblems(pwd, me.display_name, me.email) : [];
  const unavailable = (e: unknown) => e instanceof ApiError && (e.status === 404 || e.status === 503);

  return (
    <div className="space-y-6">
      <Notice tone="warn" title="Внутрішній ключ — не КЕП">
        Він діє лише в межах АІ-СЕД ПУЕТ для внутрішніх віз і погоджень. Підпис, що надає документу юридичної сили, накладається тільки КЕП кваліфікованого надавача (Дія, ПриватБанк, інші КНЕДП).
      </Notice>

      <section aria-labelledby="kf-h" className="space-y-3">
        <h3 id="kf-h" className="font-semibold">Отримати файл-ключ (.p12)</h3>
        <p className="text-sm text-slate-600 dark:text-slate-400">Файл видається один раз: збережіть його в надійному місці. Пароль сервер не зберігає — відновити його неможливо.</p>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Пароль до файлу-ключа" required hint="Щонайменше 12 символів, три види символів, без імені й пошти" error={problems.length ? `Потрібно: ${problems.join("; ")}` : null}>
            {(p) => <Input {...p} type="password" autoComplete="new-password" value={pwd} onChange={(e) => setPwd(e.target.value)} />}
          </Field>
          <Field label="Повторіть пароль" required error={pwd2 && pwd2 !== pwd ? "Паролі не збігаються" : null}>
            {(p) => <Input {...p} type="password" autoComplete="new-password" value={pwd2} onChange={(e) => setPwd2(e.target.value)} />}
          </Field>
        </div>
        <ActionButton variant="primary" disabled={!pwd || problems.length > 0 || pwd !== pwd2} onAction={async () => {
          try {
            const { blob, fileName } = await kepApi.keyFile(pwd);
            downloadBlob(blob, fileName ?? "sed-puet-key.p12");
          } catch (e) {
            if (unavailable(e)) throw new Error("Внутрішній ЦСК вимкнено на цьому сервері");
            throw e;
          }
          setPwd("");
          setPwd2("");
          toast("Файл-ключ збережено. Ключ закріплено за вами");
          await qc.invalidateQueries({ queryKey: ["kep-keys"] });
        }}>Згенерувати і завантажити</ActionButton>
      </section>

      <section aria-labelledby="csr-h" className="space-y-3">
        <h3 id="csr-h" className="font-semibold">Або: сертифікат за запитом CSR (ключ створюєте ви)</h3>
        <p className="text-sm text-slate-600 dark:text-slate-400">
          Виконайте на своєму комп'ютері <Mono>python -m sed_routing.keytool new-key --key my.key --csr my.csr</Mono> і вставте вміст <Mono>my.csr</Mono>. Ім'я в сертифікаті сервер візьме з вашого облікового запису.
        </p>
        <Field label="Запит CSR (PEM)">
          {(p) => <Textarea {...p} rows={6} className="font-mono text-xs" placeholder="-----BEGIN CERTIFICATE REQUEST-----" value={csr} onChange={(e) => setCsr(e.target.value)} />}
        </Field>
        <Field label="…або з файлу">{(p) => <Input {...p} type="file" accept=".csr,.pem,.req" onChange={async (e) => { const f = e.target.files?.[0]; if (f) setCsr(await f.text()); }} />}</Field>
        <ActionButton disabled={csr.trim().length < 100} onAction={async () => {
          const c = await kepApi.enrollCsr(csr.trim());
          setIssued(c);
          setCsr("");
          await qc.invalidateQueries({ queryKey: ["kep-keys"] });
        }}>Отримати сертифікат</ActionButton>
        {issued && (
          <div className="space-y-2 rounded-md border border-emerald-300 p-3 dark:border-emerald-800">
            <Notice tone="ok">{issued.notice_uk}</Notice>
            <KV items={[["Власник", issued.subject_cn], ["Серійний номер", <Mono key="s">{issued.serial}</Mono>], ["Діє", `${fmtDate(issued.valid_from)} — ${fmtDate(issued.valid_to)}`]]} />
            <div className="flex flex-wrap gap-2">
              <ActionButton size="sm" onAction={() => downloadBlob(new Blob([issued.certificate_pem], { type: "application/x-pem-file" }), `sed-puet-${issued.serial.slice(0, 8)}.crt`)}>Завантажити сертифікат</ActionButton>
              <ActionButton size="sm" onAction={() => downloadBlob(new Blob([issued.chain_pem], { type: "application/x-pem-file" }), "sed-puet-ca-chain.pem")}>Ланцюжок ЦСК</ActionButton>
            </div>
          </div>
        )}
      </section>

      <section aria-labelledby="ca-h" className="space-y-2">
        <h3 id="ca-h" className="font-semibold">Внутрішній ЦСК</h3>
        <div className="flex flex-wrap gap-2">
          <ActionButton size="sm" onAction={async () => { const r = await kepApi.caChain(); downloadBlob(r.blob, "sed-puet-ca-chain.pem"); }}>Ланцюжок сертифікатів ЦСК (.pem)</ActionButton>
          <ActionButton size="sm" onAction={async () => { const r = await kepApi.crl(); downloadBlob(r.blob, "sed-puet.crl"); }}>Список відкликаних (CRL)</ActionButton>
        </div>
        <p className="text-xs text-slate-600 dark:text-slate-400">Відкликати внутрішній ключ можна в розділі «Мої ключі» (кнопка «Відкріпити»): сертифікат потрапить у CRL.</p>
      </section>
    </div>
  );
}
