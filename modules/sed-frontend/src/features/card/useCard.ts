import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { ApiError } from "../../api/client";
import { documentsApi } from "../../api/endpoints";
import type { DocumentCard } from "../../api/types";
import { useToast } from "../../components/toast";

export function useCard(id: string) {
  return useQuery({ queryKey: ["card", id], queryFn: () => documentsApi.card(id) });
}

/**
 * Обгортка дій над карткою: після успіху — оновити картку, черги і реєстр;
 * на 409 (картку змінив хтось інший, row_version) — сказати про це і перечитати картку.
 */
export function useCardAction(card: DocumentCard) {
  const qc = useQueryClient();
  const toast = useToast();
  const refresh = useCallback(async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["card", card.id] }),
      qc.invalidateQueries({ queryKey: ["route", card.id] }),
      qc.invalidateQueries({ queryKey: ["review-queue"] }),
      qc.invalidateQueries({ queryKey: ["inbox"] }),
      qc.invalidateQueries({ queryKey: ["documents"] }),
    ]);
  }, [qc, card.id]);
  const run = useCallback(
    async (fn: () => Promise<unknown>, success?: string) => {
      try {
        await fn();
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          toast(`${e.message}. Картку оновлено — перевірте зміни і повторіть дію.`, "danger");
          await refresh();
          return;
        }
        throw e;
      }
      if (success) toast(success);
      await refresh();
    },
    [refresh, toast],
  );
  return { run, refresh };
}
