/**
 * Вибір засобу підпису і створення відокремленого CAdES над SHA-256, який повідомив сервер.
 * Використовується і для візи з КЕП, і для доказу володіння ключем при закріпленні.
 */
import { useId, useState } from "react";
import { routingApi } from "../api/endpoints";
import { useApp } from "../app/context";
import { ActionButton, Field, Input, Mono, Notice, Pill } from "../components/ui";
import { signatureFileToBase64, toBase64 } from "./encoding";
import { availableSigners, type SignResult, type SigningTarget } from "./signer";

export function SignaturePicker({
  target,
  actionLabel,
  onSigned,
}: {
  target: SigningTarget;
  actionLabel: string;
  onSigned: (r: SignResult) => Promise<void>;
}) {
  const { dict } = useApp();
  const signers = availableSigners(target, { mock: dict.modules.kep_mock });
  const [choice, setChoice] = useState(signers[0]?.id ?? "file");
  const [sigFile, setSigFile] = useState<File | null>(null);
  const [keyFile, setKeyFile] = useState<File | null>(null);
  const [password, setPassword] = useState("");
  const name = useId();
  const current = signers.find((s) => s.id === choice);

  if (!signers.length) return <Notice tone="warn">Немає засобу підпису, який прийме сервер для цього кроку.</Notice>;

  const sign = async (): Promise<SignResult> => {
    if (choice === "file") {
      if (!sigFile) throw new Error("Оберіть файл підпису .p7s");
      return { signatureBase64: await signatureFileToBase64(sigFile), provider: "file_key" };
    }
    if (choice === "pkcs12") {
      if (!keyFile) throw new Error("Оберіть файл-ключ .p12");
      if (!password) throw new Error("Введіть пароль до ключа");
      // модулі криптографії завантажуються лише тут: вони важкі й потрібні не всім
      const [{ loadPkcs12 }, { signDetachedCadesBes }] = await Promise.all([import("./pkcs12"), import("./cades")]);
      const key = await loadPkcs12(await keyFile.arrayBuffer(), password);
      if (key.notAfter < new Date()) throw new Error("Строк дії сертифіката ключа минув");
      const p7s = await signDetachedCadesBes(key, { contentSha256: target.contentSha256 });
      setPassword("");
      return { signatureBase64: toBase64(p7s), provider: "file_key" };
    }
    if (choice === "mock" && target.stepId) {
      const m = await routingApi.mockSignature(target.stepId);
      return { signatureBase64: m.signature_base64, provider: "file_key" };
    }
    if (current?.sign) return current.sign(target);
    throw new Error("Засіб підпису не налаштовано");
  };

  return (
    <div className="space-y-3">
      <div className="text-sm">
        Підписується: <b>{target.description}</b>
        <div className="mt-1">SHA-256: <Mono>{target.contentSha256}</Mono></div>
      </div>
      <fieldset className="space-y-2">
        <legend className="mb-1 text-sm font-medium">Чим підписати</legend>
        {signers.map((s) => (
          <label key={s.id} className="flex items-start gap-2 text-sm">
            <input type="radio" name={name} value={s.id} checked={choice === s.id} onChange={() => setChoice(s.id)} className="mt-1" />
            <span>
              <span className="font-medium">{s.label}</span>{" "}
              <Pill tone={s.level === "qualified" ? "ok" : s.level === "mock" ? "warn" : "accent"}>
                {{ qualified: "КЕП", internal: "не КЕП", any: "КЕП або внутрішній", mock: "без юридичної сили" }[s.level]}
              </Pill>
              <span className="block text-xs text-slate-600 dark:text-slate-400">{s.hint}</span>
            </span>
          </label>
        ))}
      </fieldset>
      {choice === "file" && (
        <Field label="Файл підпису" hint="Відокремлений CAdES (.p7s, .sig) або base64-текст">
          {(p) => <Input {...p} type="file" accept=".p7s,.sig,.p7m,.txt,application/pkcs7-signature" onChange={(e) => setSigFile(e.target.files?.[0] ?? null)} />}
        </Field>
      )}
      {choice === "pkcs12" && (
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Файл-ключ (.p12)">
            {(p) => <Input {...p} type="file" accept=".p12,.pfx,application/x-pkcs12" onChange={(e) => setKeyFile(e.target.files?.[0] ?? null)} />}
          </Field>
          <Field label="Пароль до ключа" hint="Не передається на сервер">
            {(p) => <Input {...p} type="password" autoComplete="off" value={password} onChange={(e) => setPassword(e.target.value)} />}
          </Field>
        </div>
      )}
      {current && (
        <ActionButton variant="primary" onAction={async () => onSigned(await sign())}>
          {actionLabel}
        </ActionButton>
      )}
    </div>
  );
}
