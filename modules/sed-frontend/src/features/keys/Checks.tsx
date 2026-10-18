/** Перевірка сертифіката (без особистого ключа) і перевірка підпису над файлом або гешем. */
import { useState } from "react";
import { kepApi } from "../../api/endpoints";
import type { CertificateCheck, SignatureVerify } from "../../api/types";
import { fmtDateTime } from "../../domain/format";
import { sha256Hex, signatureFileToBase64 } from "../../kep/encoding";
import { Status } from "../../components/status";
import { ActionButton, Field, Input, Mono, Notice, Pill } from "../../components/ui";

/** Звіт перевірки — довільний JSON сервера; показуємо як вкладений список «ключ: значення». */
function ReportTree({ data }: { data: unknown }) {
  if (data === null || data === undefined) return <span className="text-slate-500">—</span>;
  if (typeof data !== "object") {
    const s = String(data);
    if (/^\d{4}-\d{2}-\d{2}T/.test(s)) return <>{fmtDateTime(s)}</>;
    return s.length > 40 && /^[0-9a-fA-F]+$/.test(s) ? <Mono>{s}</Mono> : <>{s}</>;
  }
  if (Array.isArray(data)) return data.length ? <ul className="ml-4 list-disc">{data.map((x, i) => <li key={i}><ReportTree data={x} /></li>)}</ul> : <span className="text-slate-500">—</span>;
  return (
    <dl className="grid grid-cols-[minmax(8rem,auto)_1fr] gap-x-4 gap-y-1 text-sm">
      {Object.entries(data as Record<string, unknown>).map(([k, v]) => (
        <div key={k} className="contents"><dt className="text-slate-500">{k}</dt><dd className="break-words"><ReportTree data={v} /></dd></div>
      ))}
    </dl>
  );
}

export function InspectCertificate() {
  const [file, setFile] = useState<File | null>(null);
  const [r, setR] = useState<CertificateCheck | null>(null);
  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-600 dark:text-slate-400">Завантажте лише сертифікат (.cer, .crt, .pem) — не файл з особистим ключем. Сервер відмовить, якщо отримає ключ.</p>
      <Field label="Сертифікат">{(p) => <Input {...p} type="file" accept=".cer,.crt,.pem,.der" onChange={(e) => { setFile(e.target.files?.[0] ?? null); setR(null); }} />}</Field>
      <ActionButton variant="primary" disabled={!file} onAction={async () => setR(await kepApi.inspectCertificate(file!))}>Перевірити</ActionButton>
      {r && (
        <div className="space-y-3" aria-live="polite">
          <div className="flex flex-wrap items-center gap-2">
            <Pill tone={r.level === "qualified" ? "ok" : r.level === "other" ? "danger" : "accent"}>{r.level_uk}</Pill>
            {r.usable_for_signing ? <Pill tone="ok">придатний для підпису</Pill> : <Pill tone="danger">не придатний для підпису</Pill>}
            {r.trusted_issuer === true && <Pill tone="ok">КНЕДП довірений</Pill>}
            {r.trusted_issuer === false && <Pill tone="danger">видавець не в довірчому списку</Pill>}
            {r.trusted_issuer === null && <Pill tone="warn">ДСТУ: ланцюжок перевіряє провайдер КНЕДП</Pill>}
            {r.registered_to_me && <Pill tone="ok">закріплено за вами</Pill>}
          </div>
          {r.problems.length > 0 && <Notice tone="warn" title="Зауваження"><ul className="ml-5 list-disc">{r.problems.map((p) => <li key={p}>{p}</li>)}</ul></Notice>}
          <ReportTree data={r.certificate} />
        </div>
      )}
    </div>
  );
}

export function VerifySignature() {
  const [sig, setSig] = useState<File | null>(null);
  const [doc, setDoc] = useState<File | null>(null);
  const [hash, setHash] = useState("");
  const [r, setR] = useState<SignatureVerify | null>(null);
  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-600 dark:text-slate-400">Перевірка відокремленого підпису CAdES: структура, геш, ланцюжок до КНЕДП, відкликання. Підписаний файл на сервер не надсилається — у браузері обчислюється лише його SHA-256.</p>
      <div className="grid gap-3 md:grid-cols-3">
        <Field label="Файл підпису (.p7s)" required>{(p) => <Input {...p} type="file" accept=".p7s,.sig,.p7m,.txt" onChange={(e) => { setSig(e.target.files?.[0] ?? null); setR(null); }} />}</Field>
        <Field label="Підписаний файл">{(p) => <Input {...p} type="file" onChange={async (e) => { const f = e.target.files?.[0] ?? null; setDoc(f); setR(null); if (f) setHash(await sha256Hex(await f.arrayBuffer())); }} />}</Field>
        <Field label="…або SHA-256 вмісту" hint="64 шістнадцяткові символи">{(p) => <Input {...p} className="font-mono" value={hash} onChange={(e) => { setHash(e.target.value.trim().toLowerCase()); setDoc(null); }} />}</Field>
      </div>
      {doc && <p className="text-xs">SHA-256 файлу «{doc.name}»: <Mono>{hash}</Mono></p>}
      <ActionButton variant="primary" disabled={!sig || !/^[0-9a-f]{64}$/.test(hash)} onAction={async () => setR(await kepApi.verifySignature(await signatureFileToBase64(sig!), hash))}>Перевірити підпис</ActionButton>
      {r && (
        <div className="space-y-3" aria-live="polite">
          <div className="flex flex-wrap items-center gap-2">
            <Status e="SignatureValidation" v={r.status} />
            {r.level && <Pill>{{ qualified: "КЕП", internal: "внутрішній ключ АІ-СЕД", mock: "імітація", other: "некваліфікований" }[r.level]}</Pill>}
          </div>
          {r.reasons.length > 0 && <Notice tone={r.status === "valid" ? "neutral" : "danger"}><ul className="ml-5 list-disc">{r.reasons.map((x) => <li key={x}>{x}</li>)}</ul></Notice>}
          <ReportTree data={r.report} />
        </div>
      )}
    </div>
  );
}
