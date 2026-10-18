"""Рішення людини над результатами ШІ (Human-in-the-loop).

* реквізит підтверджено / виправлено / відхилено → `ai_extracted_fields` + `documents.requisites`;
* модель не відповіла → оператор вводить реквізити вручну, запуск NER закривається як `corrected`;
* зауваження комплаєнсу прийнято / відхилено / виправлено → `compliance_findings`;
* перевірку ШІ в цілому підтверджено або відхилено → `ai_runs.human_review_status`.

Транзакція виконується від імені користувача (`sed.actor_type = user`): guard-тригер БД
дозволяє ці зміни лише людині, а тригер журналу пише хто і що змінив.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from sed_db.enums import AiTaskType, FindingStatus, HumanReviewStatus
from sed_db.models import AiExtractedField, AiRun, ComplianceCheck, ComplianceFinding, Document

from ..api.schemas import FieldDecision, FieldReviewIn, FindingResolutionIn
from ..domain.errors import Conflict, InvalidInput, NotFound
from ..domain.requisites import normalize_requisite
from .db import set_actor

_REVIEWED = (HumanReviewStatus.CONFIRMED, HumanReviewStatus.CORRECTED, HumanReviewStatus.REJECTED)
_DECISION_STATUS = {
    FieldDecision.CONFIRM: HumanReviewStatus.CONFIRMED,
    FieldDecision.CORRECT: HumanReviewStatus.CORRECTED,
    FieldDecision.REJECT: HumanReviewStatus.REJECTED,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _apply_to_card(doc: Document, key: str, value: Any | None) -> None:
    """Підтверджене значення потрапляє в картку; заголовок і короткий зміст — і в окремі колонки."""
    req = dict(doc.requisites or {})
    if value is None:
        req.pop(key, None)
    else:
        req[key] = value
    doc.requisites = req
    if key == "title" and isinstance(value, str) and value:
        doc.title = value[:1024]
    elif key == "summary":
        doc.summary = value if isinstance(value, str) else None


class SqlReviewRepository:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def _document(self, s: AsyncSession, document_id: uuid.UUID, row_version: int | None) -> Document:
        doc = await s.scalar(select(Document).where(Document.id == document_id).with_for_update())
        if doc is None:
            raise NotFound("Документ не знайдено")
        if row_version is not None and doc.row_version != row_version:
            raise Conflict("Картку щойно змінив інший користувач. Оновіть сторінку і повторіть дію")
        return doc

    async def review_field(self, user_id: uuid.UUID, document_id: uuid.UUID, field_id: uuid.UUID,
                           inp: FieldReviewIn, request_id: str | None) -> None:
        try:
            async with self.sessionmaker() as s, s.begin():
                await set_actor(s, user_id, request_id, reason="ручна валідація реквізиту ШІ")
                doc = await self._document(s, document_id, inp.row_version)
                row = (await s.execute(
                    select(AiExtractedField, AiRun)
                    .join(AiRun, AiRun.id == AiExtractedField.ai_run_id)
                    .where(AiExtractedField.id == field_id, AiRun.document_id == document_id)
                    .with_for_update(of=AiExtractedField)
                )).one_or_none()
                if row is None:
                    raise NotFound("Реквізит не знайдено в цьому документі")
                field, run = row
                if field.document_version_id != doc.current_version_id:
                    raise Conflict("Реквізит належить не поточній версії документа")

                if inp.decision is FieldDecision.CONFIRM:
                    if field.extracted_value in (None, "", [], {}):
                        raise InvalidInput("Порожнє значення не можна підтвердити: виправте його або відхиліть")
                    value = normalize_requisite(field.field_key, field.extracted_value)
                elif inp.decision is FieldDecision.CORRECT:
                    value = normalize_requisite(field.field_key, inp.value)
                else:
                    value = None

                field.validated_value = value
                field.human_review_status = _DECISION_STATUS[inp.decision]
                field.reviewed_by_id = user_id
                field.reviewed_at = _now()
                field.review_comment = (inp.comment or "").strip() or None
                _apply_to_card(doc, field.field_key, value)
                await s.flush()
                await self._close_run_if_done(s, run, user_id)
        except StaleDataError:
            raise Conflict("Картку щойно змінив інший користувач. Оновіть сторінку і повторіть дію") from None

    async def accept_confident(self, user_id: uuid.UUID, document_id: uuid.UUID, row_version: int | None,
                               request_id: str | None) -> int:
        """Перенести в картку значення, що ШІ вилучив з впевненістю ≥ порогу (`not_required`).

        Навіть упевнені значення стають реквізитами картки лише дією людини: вона бачить їх
        і підтверджує одним кліком. Порожні й некоректні за форматом значення пропускаються (їх виправляють окремо). Повертає кількість перенесених.
        """
        try:
            async with self.sessionmaker() as s, s.begin():
                await set_actor(s, user_id, request_id, reason="підтвердження впевнених реквізитів ШІ")
                doc = await self._document(s, document_id, row_version)
                rows = (await s.execute(
                    select(AiExtractedField, AiRun)
                    .join(AiRun, AiRun.id == AiExtractedField.ai_run_id)
                    .where(AiRun.document_id == document_id,
                           AiExtractedField.document_version_id == doc.current_version_id,
                           AiExtractedField.human_review_status == HumanReviewStatus.NOT_REQUIRED)
                    .with_for_update(of=AiExtractedField)
                )).all()
                n = 0
                for field, _run in rows:
                    if field.extracted_value in (None, "", [], {}):
                        continue
                    try:
                        value = normalize_requisite(field.field_key, field.extracted_value)
                    except InvalidInput:
                        continue        # модель упевнена, але формат хибний — лишається для ручного виправлення
                    field.validated_value = value
                    field.human_review_status = HumanReviewStatus.CONFIRMED
                    field.reviewed_by_id = user_id
                    field.reviewed_at = _now()
                    _apply_to_card(doc, field.field_key, value)
                    n += 1
                if not n:
                    raise InvalidInput("Немає впевнених значень ШІ, які ще не перенесено в картку")
                await s.flush()
                for run in {r for _f, r in rows}:
                    await self._close_run_if_done(s, run, user_id)
                return n
        except StaleDataError:
            raise Conflict("Картку щойно змінив інший користувач. Оновіть сторінку і повторіть дію") from None

    async def _close_run_if_done(self, s: AsyncSession, run: AiRun, user_id: uuid.UUID) -> None:
        """Усі реквізити запуску розглянуто → запуск NER вважається перевіреним людиною."""
        if run.human_review_status is not HumanReviewStatus.PENDING:
            return
        statuses = (await s.scalars(select(AiExtractedField.human_review_status)
                                    .where(AiExtractedField.ai_run_id == run.id))).all()
        if any(st is HumanReviewStatus.PENDING for st in statuses):
            return
        corrected = any(st in (HumanReviewStatus.CORRECTED, HumanReviewStatus.REJECTED) for st in statuses)
        run.human_review_status = HumanReviewStatus.CORRECTED if corrected else HumanReviewStatus.CONFIRMED
        run.reviewed_by_id = user_id
        run.reviewed_at = _now()

    async def enter_requisites(self, user_id: uuid.UUID, document_id: uuid.UUID, values: dict[str, Any],
                               row_version: int | None, request_id: str | None) -> None:
        """Ручне введення, коли ШІ не вилучив реквізити (LLM недоступна, нечитабельний скан)."""
        normalized = {k: normalize_requisite(k, v) for k, v in values.items() if v not in (None, "", [], {})}
        if not normalized:
            raise InvalidInput("Заповніть хоча б один реквізит")
        try:
            async with self.sessionmaker() as s, s.begin():
                await set_actor(s, user_id, request_id, reason="ручне введення реквізитів")
                doc = await self._document(s, document_id, row_version)
                for k, v in normalized.items():
                    _apply_to_card(doc, k, v)
                run = await s.scalar(
                    select(AiRun).where(AiRun.document_version_id == doc.current_version_id,
                                        AiRun.task_type == AiTaskType.NER_EXTRACTION,
                                        AiRun.human_review_status == HumanReviewStatus.PENDING)
                    .order_by(AiRun.queued_at.desc()).limit(1).with_for_update())
                if run is not None:
                    pending = await s.scalar(select(func.count(AiExtractedField.id)).where(
                        AiExtractedField.ai_run_id == run.id,
                        AiExtractedField.human_review_status == HumanReviewStatus.PENDING))
                    if not pending:
                        run.human_review_status = HumanReviewStatus.CORRECTED
                        run.reviewed_by_id = user_id
                        run.reviewed_at = _now()
                        run.review_comment = "Реквізити введено вручну"
        except StaleDataError:
            raise Conflict("Картку щойно змінив інший користувач. Оновіть сторінку і повторіть дію") from None

    async def resolve_finding(self, user_id: uuid.UUID, document_id: uuid.UUID, finding_id: uuid.UUID,
                              inp: FindingResolutionIn, request_id: str | None) -> None:
        async with self.sessionmaker() as s, s.begin():
            await set_actor(s, user_id, request_id, reason="розгляд зауваження комплаєнсу")
            finding = await s.scalar(
                select(ComplianceFinding)
                .join(ComplianceCheck, ComplianceCheck.id == ComplianceFinding.check_id)
                .join(AiRun, AiRun.id == ComplianceCheck.ai_run_id)
                .where(ComplianceFinding.id == finding_id, AiRun.document_id == document_id)
                .with_for_update(of=ComplianceFinding))
            if finding is None:
                raise NotFound("Зауваження не знайдено в цьому документі")
            finding.status = inp.status
            finding.resolved_by_id = user_id
            finding.resolved_at = _now()
            finding.resolution_comment = (inp.comment or "").strip() or None

    async def review_run(self, user_id: uuid.UUID, document_id: uuid.UUID, run_id: uuid.UUID,
                         decision: FieldDecision, comment: str | None, request_id: str | None) -> None:
        """Підсумкове рішення людини щодо запуску ШІ (напр. звіт комплаєнсу з низькою впевненістю)."""
        if decision is FieldDecision.CORRECT:
            raise InvalidInput("Для запуску доступні лише «підтвердити» або «відхилити»")
        async with self.sessionmaker() as s, s.begin():
            await set_actor(s, user_id, request_id, reason="ручна валідація результату ШІ")
            run = await s.scalar(select(AiRun).where(AiRun.id == run_id, AiRun.document_id == document_id)
                                 .with_for_update())
            if run is None:
                raise NotFound("Запуск ШІ не знайдено в цьому документі")
            if run.task_type is AiTaskType.COMPLIANCE_CHECK and decision is FieldDecision.CONFIRM:
                open_ = await s.scalar(
                    select(func.count(ComplianceFinding.id))
                    .join(ComplianceCheck, ComplianceCheck.id == ComplianceFinding.check_id)
                    .where(ComplianceCheck.ai_run_id == run.id, ComplianceFinding.status == FindingStatus.OPEN))
                if open_:
                    raise Conflict("Спершу розгляньте всі відкриті зауваження перевірки")
            run.human_review_status = _DECISION_STATUS[decision]
            run.reviewed_by_id = user_id
            run.reviewed_at = _now()
            run.review_comment = (comment or "").strip() or None
