import type { DocumentCard, VersionOut } from "../../api/types";
import { useApp } from "../../app/context";
import { fmtDateTime, fmtSize, shortHash } from "../../domain/format";
import { Status } from "../../components/status";
import { Button, Mono, Pill, Table, Td } from "../../components/ui";

export function VersionsTab({ card, onPreview }: { card: DocumentCard; onPreview: (v: VersionOut) => void }) {
  const { L } = useApp();
  return (
    <div className="space-y-6">
      <Table caption="Версії документа" head={["№", "Файл", "Джерело", "Створено", "SHA-256", ""]}>
        {card.versions.map((v) => (
          <tr key={v.id}>
            <Td>{v.version_number}{v.id === card.current_version_id && <div><Pill tone="accent">поточна</Pill></div>}</Td>
            <Td>{v.file_name}<div className="text-xs text-slate-500">{fmtSize(v.size_bytes)}{v.page_count ? `, ${v.page_count} стор.` : ""}</div>
              {v.is_locked && <div className="mt-1"><Pill>заблоковано для змін</Pill></div>}
              {v.change_note && <div className="text-xs">{v.change_note}</div>}</Td>
            <Td className="text-xs">{L("VersionSource", v.source)}</Td>
            <Td className="text-xs">{fmtDateTime(v.created_at)}<div className="text-slate-500">{v.created_by.name}</div></Td>
            <Td><Mono title={v.content_sha256}>{shortHash(v.content_sha256)}</Mono></Td>
            <Td><Button size="sm" onClick={() => onPreview(v)}>Переглянути</Button></Td>
          </tr>
        ))}
      </Table>
      <section aria-labelledby="sigs">
        <h3 id="sigs" className="mb-2 font-semibold">Кваліфіковані електронні підписи</h3>
        {card.signatures.length ? (
          <Table caption="Підписи" head={["Підписувач", "Сертифікат", "Формат", "Час підпису", "Статус"]}>
            {card.signatures.map((s) => (
              <tr key={s.id}>
                <Td>{s.signer.name}<div className="text-xs text-slate-500">{s.cert_subject_cn}</div></Td>
                <Td className="text-xs">{s.cert_issuer}<div><Mono>{s.cert_serial}</Mono></div></Td>
                <Td className="text-xs">{s.signature_format}</Td>
                <Td className="text-xs">{fmtDateTime(s.signed_at)}</Td>
                <Td><Status e="SignatureValidation" v={s.validation_status} />{s.validated_at && <div className="text-xs text-slate-500">перевірено {fmtDateTime(s.validated_at)}</div>}</Td>
              </tr>
            ))}
          </Table>
        ) : <p className="text-sm text-slate-600 dark:text-slate-400">Підписів ще немає.</p>}
      </section>
    </div>
  );
}
