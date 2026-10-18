"""Сервіс вилучення реквізитів: PII-маскування → LLM (structured output) → валідація → скоринг."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timezone

from pydantic import ValidationError

from ..domain.llm_schema import LlmExtraction, llm_json_schema
from ..domain.models import ExtractionOutcome, LlmCompletion, LlmError, OcrResult
from ..ports import PiiMasker, StructuredLlm
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .scoring import PAGE_SEPARATOR, score_extraction

log = logging.getLogger(__name__)

SCHEMA_NAME = "dstu4163_requisites"


class RequisitesExtractor:
    def __init__(self, llm: StructuredLlm, masker: PiiMasker, max_input_chars: int) -> None:
        self.llm = llm
        self.masker = masker
        self.max_input_chars = max_input_chars
        self.schema = llm_json_schema()

    async def extract(self, ocr: OcrResult, today: date | None = None) -> ExtractionOutcome:
        started = datetime.now(timezone.utc)
        today = today or started.date()
        text = PAGE_SEPARATOR.join(p.text for p in ocr.pages)
        warnings: list[str] = []

        if not text.strip():
            return ExtractionOutcome(
                requisites=None, masked_output=None, input_sha256=None, pii_entities_masked=0,
                completion=None, error="Текст документа не розпізнано — реквізити потрібно ввести вручну",
                started_at=started, finished_at=datetime.now(timezone.utc),
            )

        masking = self.masker.mask(text)
        user_prompt, truncated = build_user_prompt(masking.masked_text, today, self.max_input_chars)
        if truncated:
            warnings.append("Документ довгий: моделі передано початок і кінець тексту")
        # Сирий вхід моделі не зберігається, лише його SHA-256 (доказовість без ПД).
        input_sha256 = hashlib.sha256((SYSTEM_PROMPT + "\n" + user_prompt).encode()).hexdigest()

        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}]
        completion: LlmCompletion | None = None
        raw: LlmExtraction | None = None
        error: str | None = None
        try:
            completion = await self.llm.complete_json(messages, self.schema, SCHEMA_NAME)
            try:
                raw = LlmExtraction.model_validate(completion.content)
            except ValidationError as exc:
                # Одна спроба «ремонту»: модель бачить свою відповідь і помилки валідації.
                log.info("LLM output failed validation, retrying once: %s", exc.error_count())
                repair = messages + [
                    {"role": "assistant", "content": json.dumps(completion.content, ensure_ascii=False)},
                    {"role": "user", "content": "Відповідь не відповідає JSON-схемі: "
                     + _short_errors(exc) + ". Поверни виправлений JSON повністю."},
                ]
                first = completion
                completion = await self.llm.complete_json(repair, self.schema, SCHEMA_NAME)
                completion = _merge_usage(first, completion)
                raw = LlmExtraction.model_validate(completion.content)
        except LlmError as exc:
            error = exc.message
        except ValidationError as exc:
            error = "Модель повернула відповідь, що не відповідає схемі реквізитів: " + _short_errors(exc)

        requisites = None
        if raw is not None:
            requisites = score_extraction(
                raw, masking.masked_text, ocr,
                restore=lambda v: self.masker.restore(v, masking.mapping), today=today,
            )
        if ocr.truncated:
            warnings.append(f"Оброблено {len(ocr.pages)} з {ocr.page_count} сторінок")

        return ExtractionOutcome(
            requisites=requisites,
            masked_output=raw.model_dump(mode="json") if raw is not None else None,
            input_sha256=input_sha256,
            pii_entities_masked=masking.entities_masked,
            completion=completion,
            error=error,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            warnings=warnings,
            pii_counts=dict(masking.counts),
        )


def _short_errors(exc: ValidationError, limit: int = 5) -> str:
    parts = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:limit]]
    return "; ".join(parts)


def _merge_usage(a: LlmCompletion, b: LlmCompletion) -> LlmCompletion:
    def add(x: int | None, y: int | None) -> int | None:
        return None if x is None and y is None else (x or 0) + (y or 0)

    return LlmCompletion(
        content=b.content, model=b.model, tokens_in=add(a.tokens_in, b.tokens_in),
        tokens_out=add(a.tokens_out, b.tokens_out), latency_ms=a.latency_ms + b.latency_ms,
        attempts=a.attempts + b.attempts,
    )
