"""Граф нормативного аудиту (LangGraph).

```
START → prepare → check_references → retrieve ─┬─(слабкий пошук, 1 раз)→ rewrite_queries → retrieve
                                               └→ assess ─┬─(є зауваження)→ verify_findings → finalize → END
                                                          └→ finalize → END
```

* **prepare** — псевдонімізація ПД, розбір проєкту на пункти (констатувальна частина, пункти
  розпорядчої частини, підстава), посада підписанта.
* **check_references** — детермінована перевірка посилань на акти (скасований / ще не чинний).
* **retrieve** — гібридний пошук норм для кожного пункту (dense + BM25 → RRF → reranker), лише
  чинні на дату перевірки.
* **rewrite_queries** (Corrective RAG) — пункти зі слабким збігом LLM переформульовує мовою
  нормативних актів і шукає ще раз (не більше одного разу).
* **assess** — LLM оцінює кожен пункт проти знайдених норм (structured output, мітки N1…N6).
* **verify_findings** — незалежна повторна перевірка кожного зауваження окремим викликом
  (лише пункт + одна норма), щоб відсіяти хибні спрацювання.
* **finalize** — звірка цитат з текстом норм і проєкту, детермінований confidence, вердикт, звіт.

ШІ не змінює статус документа: звіт лише інформує людину, яка погоджує і підписує КЕП.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, TypedDict, TypeVar

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError

from sed_db.base import CONFIDENCE_THRESHOLD
from sed_db.enums import ComplianceVerdict, FindingCategory, FindingSeverity, RegulatoryStatus
from sed_ingest.domain.models import LlmCompletion, LlmError

from ..config import Settings
from ..domain.models import (
    CandidateFinding,
    Coverage,
    InvalidDraft,
    Provision,
    ProvisionKind,
    RetrievedClause,
)
from ..domain.report import (
    CATEGORY_LABELS_UK,
    SEVERITY_LABELS_UK,
    VERDICT_LABELS_UK,
    Citation,
    ComplianceReport,
    DraftFragment,
    Finding,
    ProvisionCoverage,
    ViolatedClause,
)
from ..ports import PiiMasker, StructuredLlm, VectorIndex
from ..retrieval.hybrid import HybridRetriever, RetrievalResult
from .draft import ParsedDraft, parse_draft
from .llm_schema import LlmAssessment, LlmQueryRewrite, LlmVerification, schema_of
from .prompts import (
    ASSESS_SYSTEM,
    PROMPT_BODIES,
    REWRITE_SYSTEM,
    VERIFY_SYSTEM,
    build_assess_prompt,
    build_rewrite_prompt,
    build_verify_prompt,
    retrieval_query,
)
from .references import extract_references, is_outdated, match_reference
from .scoring import q4, score_finding, score_no_issue
from .verification import QuoteMatch, first_sentences, locate

log = logging.getLogger(__name__)
M = TypeVar("M", bound=BaseModel)

_SEVERITY_ORDER = {FindingSeverity.CRITICAL: 0, FindingSeverity.MAJOR: 1,
                   FindingSeverity.MINOR: 2, FindingSeverity.INFO: 3}


@dataclass(slots=True)
class LlmTrace:
    """Що пішло в модель і що вона повернула — для `ai_runs` (лише мітки ПД, без сирого тексту)."""

    prompts_sha: list[str] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    failures: int = 0

    def add(self, key: str, system: str, user: str, completion: LlmCompletion | None, parsed: Any) -> None:
        self.prompts_sha.append(hashlib.sha256(f"{system}\n{user}".encode()).hexdigest())
        if completion is not None:
            self.calls += completion.attempts
            self.tokens_in += completion.tokens_in or 0
            self.tokens_out += completion.tokens_out or 0
        self.outputs[key] = parsed

    @property
    def input_sha256(self) -> str:
        return hashlib.sha256("".join(self.prompts_sha).encode()).hexdigest()


class AuditState(TypedDict, total=False):
    draft_text: str
    as_of: date
    masked: str
    mapping: dict[str, str]
    pii_count: int
    parsed: ParsedDraft
    queries: dict[str, str]
    pending: list[str]
    retrievals: dict[str, RetrievalResult]
    rewritten: list[str]
    rewrite_done: bool
    assessments: dict[str, LlmAssessment]
    failed: dict[str, str]
    candidates: list[CandidateFinding]
    ref_findings: list[CandidateFinding]
    warnings: list[str]
    sources_considered: int
    report: ComplianceReport


@dataclass(frozen=True, slots=True)
class AuditOutcome:
    report: ComplianceReport
    trace: LlmTrace
    pii_entities_masked: int
    latency_ms: int


class ComplianceAuditor:
    """Збирає граф з портів; один екземпляр обслуговує багато перевірок."""

    def __init__(self, *, retriever: HybridRetriever, index: VectorIndex, llm: StructuredLlm,
                 masker: PiiMasker, settings: Settings) -> None:
        self.retriever = retriever
        self.index = index
        self.llm = llm
        self.masker = masker
        self.s = settings
        self._schemas = {m: schema_of(m) for m in (LlmAssessment, LlmVerification, LlmQueryRewrite)}
        self.graph = self._build()

    # --- публічний API ------------------------------------------------------------------

    async def audit(self, draft_text: str, as_of: date) -> AuditOutcome:
        if not draft_text or not draft_text.strip():
            raise InvalidDraft("Текст проєкту порожній")
        if len(draft_text) > self.s.max_draft_chars:
            raise InvalidDraft(f"Проєкт задовгий ({len(draft_text)} символів, ліміт {self.s.max_draft_chars})")
        started = time.monotonic()
        trace = LlmTrace()
        state = await self.graph.ainvoke(
            {"draft_text": draft_text, "as_of": as_of, "warnings": []},
            config={"configurable": {"trace": trace}, "recursion_limit": 25},
        )
        return AuditOutcome(state["report"], trace, state.get("pii_count", 0),
                            int((time.monotonic() - started) * 1000))

    def prompts(self) -> list[tuple[str, int, str]]:
        return [(code, ver, body) for code, (ver, body) in PROMPT_BODIES.items()]

    # --- граф ---------------------------------------------------------------------------

    def _build(self) -> Any:
        g = StateGraph(AuditState)
        g.add_node("prepare", self._prepare)
        g.add_node("check_references", self._check_references)
        g.add_node("retrieve", self._retrieve)
        g.add_node("rewrite_queries", self._rewrite_queries)
        g.add_node("assess", self._assess)
        g.add_node("verify_findings", self._verify_findings)
        g.add_node("finalize", self._finalize)
        g.add_edge(START, "prepare")
        g.add_edge("prepare", "check_references")
        g.add_edge("check_references", "retrieve")
        g.add_conditional_edges("retrieve", self._after_retrieve,
                                {"rewrite": "rewrite_queries", "assess": "assess"})
        g.add_edge("rewrite_queries", "retrieve")
        g.add_conditional_edges("assess", self._after_assess,
                                {"verify": "verify_findings", "finalize": "finalize"})
        g.add_edge("verify_findings", "finalize")
        g.add_edge("finalize", END)
        return g.compile()

    async def _prepare(self, st: AuditState) -> dict[str, Any]:
        masking = self.masker.mask(st["draft_text"])
        parsed = parse_draft(masking.masked_text, max_provisions=self.s.max_provisions,
                             max_chars=self.s.provision_max_chars)
        warnings = list(st.get("warnings", []))
        if not parsed.structured:
            warnings.append("Розпорядчу частину («НАКАЗУЮ:») не знайдено: текст перевірено за абзацами")
        if not parsed.provisions:
            warnings.append("У проєкті не знайдено пунктів для перевірки")
        queries = {p.id: retrieval_query(parsed.context, p) for p in parsed.provisions}
        return {"masked": masking.masked_text, "mapping": masking.mapping, "pii_count": masking.entities_masked,
                "parsed": parsed, "queries": queries, "pending": list(queries), "retrievals": {},
                "rewritten": [], "rewrite_done": False, "warnings": warnings}

    async def _check_references(self, st: AuditState) -> dict[str, Any]:
        refs = extract_references(st["masked"])
        if not refs:
            return {"ref_findings": []}
        catalog = await self.index.catalog()
        provisions = st["parsed"].provisions
        findings: list[CandidateFinding] = []
        warnings = list(st["warnings"])
        seen: set[str] = set()
        for ref in refs:
            m = match_reference(ref, catalog, st["as_of"])
            if m is None:
                key = ref.title.casefold()
                if key not in seen:
                    seen.add(key)
                    warnings.append(f"Посилання «{ref.text}» не перевірено: акта немає в нормативному корпусі")
                continue
            if not is_outdated(m):
                continue
            meta = m.source.meta
            prov = next((p for p in provisions if ref.text in p.text), provisions[0] if provisions else None)
            if prov is None:
                continue
            clause = RetrievedClause(
                chunk_id=m.source.first_chunk_id, source_id=m.source.source_id, act=meta,
                section_ref=m.source.first_chunk_ref, heading_path=(), content=m.source.first_chunk_text,
                text_quality=1.0, dense_score=None, sparse_score=None, fused_score=0.0,
                dense_rank=None, sparse_rank=None,
            )
            if meta.status is RegulatoryStatus.REPEALED:
                why = f"втратив чинність{f' з {meta.effective_to:%d.%m.%Y}' if meta.effective_to else ''}"
            elif meta.status is RegulatoryStatus.DRAFT:
                why = "є лише проєктом і не набрав чинності"
            else:
                why = (f"набирає чинності з {meta.effective_from:%d.%m.%Y}" if meta.effective_from
                       and meta.effective_from > st["as_of"] else "не чинний на дату перевірки")
            findings.append(CandidateFinding(
                provision_id=prov.id, category=FindingCategory.OUTDATED_REFERENCE,
                severity=FindingSeverity.MAJOR,
                title=f"Посилання на нечинний акт: {meta.label}",
                explanation=(f"Проєкт посилається на «{ref.text}». За даними нормативного корпусу акт "
                             f"«{meta.title}» {why} (перевірка станом на {st['as_of']:%d.%m.%Y})."),
                recommendation="Замініть посилання на чинний акт або перевірте реквізити акта.",
                draft_quote=ref.text, norm_quote=first_sentences(m.source.first_chunk_text, 300),
                clause=clause, llm_confidence=m.score, rank_in_retrieval=1, origin="reference_check",
            ))
        return {"ref_findings": findings, "warnings": warnings}

    async def _retrieve(self, st: AuditState) -> dict[str, Any]:
        sem = asyncio.Semaphore(self.s.llm_concurrency * 2)
        retrievals = dict(st["retrievals"])

        async def one(pid: str) -> None:
            async with sem:
                res = await self.retriever.retrieve(st["queries"][pid], st["as_of"])
            prev = retrievals.get(pid)
            retrievals[pid] = res if prev is None else _better(prev, res)

        await asyncio.gather(*(one(pid) for pid in st["pending"]))
        return {"retrievals": retrievals, "pending": []}

    def _after_retrieve(self, st: AuditState) -> str:
        if not self.s.rewrite_weak_queries or st["rewrite_done"]:
            return "assess"
        weak = [pid for pid, r in st["retrievals"].items() if r.coverage is not Coverage.STRONG]
        return "rewrite" if weak else "assess"

    async def _rewrite_queries(self, st: AuditState, config: RunnableConfig) -> dict[str, Any]:
        trace: LlmTrace = config["configurable"]["trace"]
        weak_ids = {pid for pid, r in st["retrievals"].items() if r.coverage is not Coverage.STRONG}
        weak = [p for p in st["parsed"].provisions if p.id in weak_ids]
        user = build_rewrite_prompt(st["parsed"].context, weak)
        try:
            parsed, completion = await self._call(REWRITE_SYSTEM, user, LlmQueryRewrite, "rewrite_query")
        except (LlmError, ValidationError) as exc:
            trace.failures += 1
            log.warning("query rewrite failed: %s", exc)
            return {"rewrite_done": True, "pending": []}
        trace.add("rewrite", REWRITE_SYSTEM, user, completion, parsed.model_dump(mode="json"))
        queries = dict(st["queries"])
        pending: list[str] = []
        for q in parsed.queries:
            if q.provision_id in weak_ids and q.query.strip():
                queries[q.provision_id] = f"{q.query.strip()}. {queries[q.provision_id]}"[:2000]
                pending.append(q.provision_id)
        return {"queries": queries, "pending": pending, "rewritten": pending, "rewrite_done": True}

    async def _assess(self, st: AuditState, config: RunnableConfig) -> dict[str, Any]:
        trace: LlmTrace = config["configurable"]["trace"]
        parsed = st["parsed"]
        sem = asyncio.Semaphore(self.s.llm_concurrency)
        assessments: dict[str, LlmAssessment] = {}
        failed: dict[str, str] = {}
        candidates: list[CandidateFinding] = []
        warnings = list(st["warnings"])

        async def one(p: Provision) -> None:
            clauses = st["retrievals"][p.id].clauses
            if not clauses:
                return
            user = build_assess_prompt(parsed.context, p, clauses, st["as_of"], self.s.norm_max_chars)
            try:
                async with sem:
                    res, completion = await self._call(ASSESS_SYSTEM, user, LlmAssessment, "provision_assessment")
            except LlmError as exc:
                trace.failures += 1
                failed[p.id] = exc.message
                return
            except ValidationError:
                trace.failures += 1
                failed[p.id] = "Модель повернула відповідь, що не відповідає схемі"
                return
            trace.add(f"assess:{p.id}", ASSESS_SYSTEM, user, completion, res.model_dump(mode="json"))
            assessments[p.id] = res
            for issue in res.issues:
                idx = _norm_index(issue.norm_id)
                if idx is None or not 1 <= idx <= len(clauses):
                    warnings.append(f"{p.label}: модель послалася на неіснуючу норму «{issue.norm_id}»; "
                                    "зауваження відкинуто")
                    continue
                candidates.append(CandidateFinding(
                    provision_id=p.id, category=FindingCategory(issue.category.value),
                    severity=FindingSeverity(issue.severity.value), title=issue.title.strip(),
                    explanation=issue.explanation.strip(),
                    recommendation=(issue.recommendation or "").strip() or None,
                    draft_quote=issue.draft_quote, norm_quote=issue.norm_quote, clause=clauses[idx - 1],
                    llm_confidence=issue.confidence, rank_in_retrieval=idx,
                ))

        await asyncio.gather(*(one(p) for p in parsed.provisions))
        return {"assessments": assessments, "failed": failed, "candidates": candidates, "warnings": warnings}

    def _after_assess(self, st: AuditState) -> str:
        return "verify" if st["candidates"] else "finalize"

    async def _verify_findings(self, st: AuditState, config: RunnableConfig) -> dict[str, Any]:
        trace: LlmTrace = config["configurable"]["trace"]
        provisions = {p.id: p for p in st["parsed"].provisions}
        sem = asyncio.Semaphore(self.s.llm_concurrency)

        async def one(i: int, f: CandidateFinding) -> CandidateFinding:
            user = build_verify_prompt(st["parsed"].context, f.draft_quote, provisions[f.provision_id].text,
                                       f.clause, f.title, f.explanation, st["as_of"], self.s.norm_max_chars)
            try:
                async with sem:
                    res, completion = await self._call(VERIFY_SYSTEM, user, LlmVerification, "finding_verification")
            except (LlmError, ValidationError) as exc:
                trace.failures += 1
                log.warning("verification failed: %s", exc)
                return f
            trace.add(f"verify:{i}", VERIFY_SYSTEM, user, completion, res.model_dump(mode="json"))
            f.verifier_verdict = res.verdict.value
            f.verifier_confidence = res.confidence
            f.verifier_reason = res.reason.strip()
            return f

        verified = await asyncio.gather(*(one(i, f) for i, f in enumerate(st["candidates"])))
        return {"candidates": list(verified)}

    async def _finalize(self, st: AuditState) -> dict[str, Any]:
        return {"report": self._build_report(st)}

    # --- LLM з одним «ремонтом» ------------------------------------------------------------

    async def _call(self, system: str, user: str, model: type[M], name: str) -> tuple[M, LlmCompletion]:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        schema = self._schemas[model]
        completion = await self.llm.complete_json(messages, schema, name)
        try:
            return model.model_validate(completion.content), completion
        except ValidationError as exc:
            errors = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
            repair = messages + [
                {"role": "assistant", "content": json.dumps(completion.content, ensure_ascii=False)},
                {"role": "user", "content": f"Відповідь не відповідає JSON-схемі: {errors}. Поверни виправлений JSON повністю."},
            ]
            second = await self.llm.complete_json(repair, schema, name)
            merged = LlmCompletion(
                content=second.content, model=second.model,
                tokens_in=(completion.tokens_in or 0) + (second.tokens_in or 0),
                tokens_out=(completion.tokens_out or 0) + (second.tokens_out or 0),
                latency_ms=completion.latency_ms + second.latency_ms,
                attempts=completion.attempts + second.attempts,
            )
            return model.model_validate(second.content), merged

    # --- звіт -------------------------------------------------------------------------------

    def _build_report(self, st: AuditState) -> ComplianceReport:
        parsed: ParsedDraft = st["parsed"]
        as_of: date = st["as_of"]
        mapping = st["mapping"]
        draft = st["draft_text"]
        restore = lambda s: self.masker.restore(s, mapping)  # noqa: E731
        provisions = {p.id: p for p in parsed.provisions}
        warnings = list(st["warnings"])
        failed: dict[str, str] = st.get("failed", {})
        assessments: dict[str, LlmAssessment] = st.get("assessments", {})
        retrievals: dict[str, RetrievalResult] = st.get("retrievals", {})

        # 1. Звірка цитат і скоринг.
        scored: list[tuple[CandidateFinding, float, list[str], str, bool, DraftFragment]] = []
        for f in [*st.get("ref_findings", []), *st.get("candidates", [])]:
            loc = locate(f.norm_quote, f.clause.content)
            quoted = loc.text if loc.match is not QuoteMatch.NONE and loc.text else first_sentences(f.clause.content)
            draft_quote = restore(f.draft_quote)
            dloc = locate(draft_quote, draft)
            draft_found = dloc.match in (QuoteMatch.EXACT, QuoteMatch.FUZZY)
            conf, issues = score_finding(f, loc.match, draft_found)
            prov = provisions[f.provision_id]
            fragment = DraftFragment(
                provision_id=prov.id, provision_label=prov.label,
                text=dloc.text if draft_found else draft_quote,
                char_start=dloc.start if draft_found else None, char_end=dloc.end if draft_found else None,
            )
            scored.append((f, conf, issues, quoted, quoted in f.clause.content, fragment))

        # 2. Дублікати: одна норма + один пункт проєкту → лишається найвпевненіше зауваження.
        best: dict[tuple[str, Any, FindingCategory], int] = {}
        for i, (f, conf, *_rest) in enumerate(scored):
            key = (f.provision_id, f.clause.chunk_id, f.category)
            if key not in best or conf > scored[best[key]][1]:
                best[key] = i
        scored = [scored[i] for i in sorted(best.values())]
        scored.sort(key=lambda x: (_SEVERITY_ORDER[x[0].severity], -x[1]))

        findings: list[Finding] = []
        for n, (f, conf, issues, quoted, verified, fragment) in enumerate(scored, start=1):
            c = f.clause
            findings.append(Finding(
                ordinal=n, severity=f.severity, severity_label=SEVERITY_LABELS_UK[f.severity],
                category=f.category, category_label=CATEGORY_LABELS_UK[f.category],
                title=restore(f.title), explanation=restore(f.explanation),
                recommendation=restore(f.recommendation) if f.recommendation else None,
                fragment=fragment, confidence=q4(conf), requires_human_review=q4(conf) < CONFIDENCE_THRESHOLD,
                issues=issues, origin=f.origin,
                verifier=({"verdict": f.verifier_verdict, "confidence": f.verifier_confidence,
                           "reason": f.verifier_reason} if f.verifier_verdict else None),
                citations=[_citation(1, c, quoted, verified)],
            ))

        # 3. Покриття пунктів.
        with_findings = {f.fragment.provision_id for f in findings}
        coverage_rows: list[ProvisionCoverage] = []
        component_conf: list[float] = [float(f.confidence) for f in findings]
        review_reasons: list[str] = []
        for p in parsed.provisions:
            r = retrievals.get(p.id)
            cov = r.coverage if r else Coverage.NONE
            a = assessments.get(p.id)
            no_issue: Decimal | None = None
            if a is not None and p.id not in with_findings:
                no_issue = q4(score_no_issue(a.no_issue_confidence, cov))
                component_conf.append(float(no_issue))
                if no_issue < CONFIDENCE_THRESHOLD:
                    review_reasons.append(f"{p.label}: висновок «суперечностей немає» має впевненість {no_issue}")
            if p.id in failed:
                component_conf.append(0.0)
                review_reasons.append(f"{p.label}: не перевірено ({failed[p.id]})")
            elif r is not None and not r.clauses and p.kind is ProvisionKind.ITEM:
                component_conf.append(0.5)
                review_reasons.append(f"{p.label}: релевантних норм у корпусі не знайдено")
            coverage_rows.append(ProvisionCoverage(
                provision_id=p.id, kind=p.kind, label=p.label, coverage=cov,
                norms_considered=[c.citation_label for c in (r.clauses if r else [])],
                assessed=a is not None, no_issue_confidence=no_issue,
                query_rewritten=p.id in st.get("rewritten", []),
            ))

        for f in findings:
            if f.requires_human_review:
                review_reasons.append(f"Зауваження {f.ordinal} «{f.title}»: впевненість {f.confidence} < {CONFIDENCE_THRESHOLD}")

        # 4. Вердикт.
        assessed_any = bool(assessments) or bool(st.get("ref_findings"))
        if not parsed.provisions or (not assessed_any and not findings):
            verdict = ComplianceVerdict.INCONCLUSIVE
        elif findings:
            serious = any(f.severity in (FindingSeverity.MAJOR, FindingSeverity.CRITICAL)
                          and f.confidence >= CONFIDENCE_THRESHOLD for f in findings)
            verdict = ComplianceVerdict.NON_COMPLIANT if serious else ComplianceVerdict.ISSUES_FOUND
        elif failed:
            verdict = ComplianceVerdict.INCONCLUSIVE
        else:
            verdict = ComplianceVerdict.COMPLIANT
        if verdict is ComplianceVerdict.INCONCLUSIVE:
            component_conf.append(0.0)
            if not review_reasons:
                review_reasons.append("Перевірку не завершено: жоден пункт не оцінено")
        confidence = q4(min(component_conf) if component_conf else 0.0)

        all_clauses = {c.chunk_id: c for r in retrievals.values() for c in r.clauses}
        sources = {c.source_id for c in all_clauses.values()} | {f.clause.source_id for f, *_ in scored}
        if any(c.act.is_sample for c in all_clauses.values()):
            warnings.append("У корпусі є демонстраційні тексти актів (позначені is_sample): "
                            "замініть їх офіційними редакціями перед використанням")

        violated = _violated(findings)
        report = ComplianceReport(
            verdict=verdict, verdict_label=VERDICT_LABELS_UK[verdict],
            summary=_summary(verdict, findings, violated, len(parsed.provisions), len(all_clauses), len(sources), as_of),
            confidence=confidence, requires_human_review=confidence < CONFIDENCE_THRESHOLD or bool(review_reasons),
            review_reasons=_dedupe(review_reasons), regulations_as_of=as_of, findings=findings,
            violated_clauses=violated, provisions=coverage_rows, sources_considered=len(sources),
            retrieval_config=self.retriever.config(),
            model={"llm": f"{self.llm.identity.name}:{self.llm.identity.version}",
                   "embeddings": f"{self.retriever.embedder.model_name}:{self.retriever.embedder.model_version}",
                   "reranker": self.retriever.reranker.model_name if self.retriever.reranker else "—"},
            warnings=_dedupe(warnings),
        )
        return report


# --- допоміжне ------------------------------------------------------------------------------

def _better(a: RetrievalResult, b: RetrievalResult) -> RetrievalResult:
    """Після переформулювання лишаємо кращий результат (не гірший за початковий)."""
    order = {Coverage.STRONG: 2, Coverage.WEAK: 1, Coverage.NONE: 0}
    if order[b.coverage] != order[a.coverage]:
        return b if order[b.coverage] > order[a.coverage] else a
    return b if (b.best_dense or 0) >= (a.best_dense or 0) else a


def _norm_index(norm_id: str) -> int | None:
    digits = "".join(ch for ch in norm_id if ch.isdigit())
    return int(digits) if digits else None


def _citation(rank: int, c: RetrievedClause, quoted: str, verified: bool) -> Citation:
    return Citation(
        rank=rank, chunk_id=c.chunk_id, source_id=c.source_id, source_title=c.act.title,
        source_short_title=c.act.short_title, doc_number=c.act.doc_number, issuer=c.act.issuer,
        level=c.act.level, source_status=c.act.status, section_ref=c.section_ref,
        citation_label=c.citation_label, quoted_text=quoted, quote_verified=verified, is_sample=c.act.is_sample,
        dense_score=_r(c.dense_score), sparse_score=_r(c.sparse_score), fused_score=_r(c.fused_score),
        rerank_score=_r(c.rerank_score),
    )


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 6)


def _violated(findings: list[Finding]) -> list[ViolatedClause]:
    groups: dict[Any, list[Finding]] = {}
    for f in findings:
        if f.category is FindingCategory.OUTDATED_REFERENCE:
            continue                  # цитується нечинний акт: він не «порушується», на нього хибне посилання
        groups.setdefault(f.citations[0].chunk_id, []).append(f)
    out = []
    for fs in groups.values():
        c = fs[0].citations[0]
        out.append(ViolatedClause(
            citation_label=c.citation_label, source_title=c.source_title, section_ref=c.section_ref,
            chunk_id=c.chunk_id, quoted_text=c.quoted_text, finding_ordinals=[f.ordinal for f in fs],
            max_severity=min((f.severity for f in fs), key=lambda s: _SEVERITY_ORDER[s]),
            max_confidence=max(f.confidence for f in fs),
        ))
    out.sort(key=lambda v: (_SEVERITY_ORDER[v.max_severity], -v.max_confidence))
    return out


def _summary(verdict: ComplianceVerdict, findings: list[Finding], violated: list[ViolatedClause],
             n_prov: int, n_norms: int, n_sources: int, as_of: date) -> str:
    head = (f"Перевірено {n_prov} пункт(ів) проєкту за {n_norms} фрагментами {n_sources} акт(ів), "
            f"чинних на {as_of:%d.%m.%Y}.")
    if verdict is ComplianceVerdict.INCONCLUSIVE:
        return head + " Автоматичну перевірку не завершено, потрібна ручна перевірка."
    if not findings:
        return head + " Суперечностей з наданими нормами не виявлено."
    by_sev: dict[str, int] = {}
    for f in findings:
        by_sev[f.severity_label] = by_sev.get(f.severity_label, 0) + 1
    sev = ", ".join(f"{k} — {v}" for k, v in by_sev.items())
    review = sum(f.requires_human_review for f in findings)
    clauses = "; ".join(v.citation_label for v in violated[:8]) + ("…" if len(violated) > 8 else "")
    tail = f" {review} з них потребують ручної валідації (впевненість < {CONFIDENCE_THRESHOLD})." if review else ""
    return f"{head} Зауважень: {len(findings)} ({sev}). Можуть бути порушені: {clauses}.{tail}"


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))

