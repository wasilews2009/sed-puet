"""Прикладний сервіс: перевірка тексту проєкту, перевірка версії документа з записом у БД, пошук норм."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from .audit.graph import ComplianceAuditor
from .domain.models import NotFound, RetrievedClause
from .domain.report import ComplianceReport
from .ports import CheckRecord, ComplianceRepository
from .retrieval.hybrid import HybridRetriever, RetrievalResult


class ComplianceService:
    def __init__(self, auditor: ComplianceAuditor, retriever: HybridRetriever,
                 repository: ComplianceRepository | None) -> None:
        self.auditor = auditor
        self.retriever = retriever
        self.repo = repository

    async def check_text(self, text: str, as_of: date | None = None) -> ComplianceReport:
        """Перевірка довільного тексту (чернетка в редакторі, згенерований проєкт). Нічого не зберігає."""
        outcome = await self.auditor.audit(text, as_of or date.today())
        return outcome.report

    async def check_version(self, keycloak_sub: uuid.UUID, version_id: uuid.UUID, *,
                            as_of: date | None = None, request_id: str | None = None) -> ComplianceReport:
        """Перевірка збереженої версії документа: результат пишеться в `ai_runs` + `compliance_*`,
        підсумок — у `audit_logs` (тригер)."""
        if self.repo is None:
            raise NotFound("Збереження перевірок не налаштовано (немає підключення до БД)")
        version = await self.repo.load_version(keycloak_sub, version_id)
        started = datetime.now(timezone.utc)
        as_of = as_of or version.document_date or date.today()
        outcome = await self.auditor.audit(version.text, as_of)
        finished = max(datetime.now(timezone.utc), started + timedelta(microseconds=1))
        llm = self.auditor.llm.identity
        saved = await self.repo.save_check(CheckRecord(
            version=version, report=outcome.report, llm_name=llm.name, llm_version=llm.version,
            llm_provider=llm.provider, prompts=self.auditor.prompts(), input_sha256=outcome.trace.input_sha256,
            pii_entities_masked=outcome.pii_entities_masked, raw_output=outcome.trace.outputs,
            tokens_in=outcome.trace.tokens_in or None, tokens_out=outcome.trace.tokens_out or None,
            latency_ms=outcome.latency_ms, started_at=started, finished_at=finished, request_id=request_id,
        ))
        return outcome.report.model_copy(update={
            "ai_run_id": saved.ai_run_id, "check_id": saved.check_id, "document_version_id": version.version_id,
        })

    async def search(self, query: str, as_of: date | None = None, k: int = 10) -> RetrievalResult:
        return await self.retriever.retrieve(query, as_of or date.today(), k=k)


def strip_content(c: RetrievedClause, max_chars: int) -> RetrievedClause:
    return c if len(c.content) <= max_chars else replace(c, content=c.content[:max_chars] + " …")
