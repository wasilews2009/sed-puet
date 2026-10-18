/** Перегляд оригіналу: файл завантажується з перевіркою доступу (і записом у журнал), показується з blob:. */
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo } from "react";
import { documentsApi } from "../../api/endpoints";
import type { DocumentCard, VersionOut } from "../../api/types";
import { fmtSize } from "../../domain/format";
import { Button, Card, ErrorBox, Loading } from "../../components/ui";

export function FilePreview({ card, version, onClose }: { card: DocumentCard; version: VersionOut; onClose: () => void }) {
  const file = useQuery({
    queryKey: ["file", version.id],
    queryFn: () => documentsApi.file(card.id, version.id),
    staleTime: Infinity,
    gcTime: 0,
  });
  const url = useMemo(() => (file.data ? URL.createObjectURL(file.data.blob) : null), [file.data]);
  useEffect(() => () => { if (url) URL.revokeObjectURL(url); }, [url]);

  return (
    <Card aria-labelledby="preview-title" className="space-y-3 xl:sticky xl:top-4 xl:self-start">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 id="preview-title" className="font-semibold">{version.file_name}</h2>
          <div className="text-xs text-slate-600 dark:text-slate-400">Версія {version.version_number} · {fmtSize(version.size_bytes)}{version.page_count ? ` · ${version.page_count} стор.` : ""}</div>
        </div>
        <div className="flex gap-2">
          {url && <a className="rounded-md border border-slate-300 px-2.5 py-1 text-sm no-underline dark:border-slate-600" href={url} download={version.file_name}>Завантажити</a>}
          <Button size="sm" onClick={onClose}>Закрити</Button>
        </div>
      </div>
      {file.isLoading && <Loading label="Завантаження оригіналу…" />}
      {file.error && <ErrorBox error={file.error} />}
      {url && (version.mime_type.startsWith("image/") ? (
        <img src={url} alt={`Скан документа: ${version.file_name}`} className="max-w-full rounded" />
      ) : (
        <iframe src={url} title={`Оригінал: ${version.file_name}`} className="h-[75vh] w-full rounded border border-slate-200 dark:border-slate-700" />
      ))}
    </Card>
  );
}
