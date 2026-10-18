"""Внутрішні об'єкти RAG-контуру та помилки домену."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from sed_db.enums import FindingCategory, FindingSeverity, RegulatoryLevel, RegulatoryStatus

# Межі дат для фільтрів чинності в Qdrant (дати зберігаються як int YYYYMMDD).
DATE_MIN = 19910824
DATE_MAX = 99991231


def date_key(d: date | None, default: int) -> int:
    return d.year * 10000 + d.month * 100 + d.day if d else default


# --- Помилки (API перетворює на HTTP-коди з повідомленням українською) -------------

class RagError(Exception):
    http_status = 500
    code = "rag_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidDraft(RagError):
    http_status = 422
    code = "invalid_draft"


class InvalidSource(RagError):
    http_status = 422
    code = "invalid_source"


class AccessDenied(RagError):
    http_status = 403
    code = "access_denied"


class NotFound(RagError):
    http_status = 404
    code = "not_found"


class ConfigurationError(RagError):
    http_status = 500
    code = "configuration_error"


class IndexUnavailable(RagError):
    http_status = 503
    code = "index_unavailable"


class EmbeddingError(RagError):
    http_status = 503
    code = "embedding_unavailable"


# --- Нормативний корпус ------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ActMeta:
    """Метадані нормативного акта (рядок `regulatory_sources`)."""

    title: str
    level: RegulatoryLevel
    issuer: str
    status: RegulatoryStatus = RegulatoryStatus.IN_FORCE
    doc_number: str | None = None
    adopted_on: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    short_title: str | None = None          # «Статут ПУЕТ» — як підписувати цитату
    source_url: str | None = None
    is_sample: bool = False                 # демонстраційний текст, не офіційна редакція

    @property
    def label(self) -> str:
        return self.short_title or self.title

    def in_force_on(self, d: date) -> bool:
        """Чинність на дату: проєкт акта не чинний ніколи; скасований — лише до `effective_to`
        (перевірка документа «заднім числом» оцінює норми, чинні на дату документа)."""
        if self.status is RegulatoryStatus.DRAFT:
            return False
        if self.status is RegulatoryStatus.REPEALED and self.effective_to is None:
            return False
        if self.effective_from and d < self.effective_from:
            return False
        return not (self.effective_to and d > self.effective_to)


@dataclass(frozen=True, slots=True)
class Clause:
    """Структурна одиниця акта (пункт / стаття / підпункт) після сегментації."""

    section_ref: str                        # «розд. 7, п. 7.1.3»
    heading_path: tuple[str, ...]           # заголовки розділів зверху вниз
    text: str
    number: str | None = None               # «7.1.3», «ст. 34»


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """Фрагмент, готовий до індексації: id = `regulatory_chunks.id` = point id у Qdrant."""

    id: uuid.UUID
    source_id: uuid.UUID
    index_revision: int
    chunk_no: int
    section_ref: str
    heading_path: tuple[str, ...]
    content: str                            # те, що цитується (без контекстного префікса)
    token_count: int
    text_quality: float = 1.0               # 1.0 — текстовий шар/текст; OCR — середня впевненість

    def embedding_text(self, act: ActMeta) -> str:
        """Текст для ембедингу: назва акта і шлях розділів дають фрагменту контекст."""
        head = " › ".join((act.label, *self.heading_path, self.section_ref))
        return f"{head}\n{self.content}"


@dataclass(frozen=True, slots=True)
class SourceRecord:
    id: uuid.UUID
    meta: ActMeta
    index_revision: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class RetrievedClause:
    """Кандидат-норма для одного пункту проєкту, зі слідом гібридного пошуку."""

    chunk_id: uuid.UUID
    source_id: uuid.UUID
    act: ActMeta
    section_ref: str
    heading_path: tuple[str, ...]
    content: str
    text_quality: float
    dense_score: float | None
    sparse_score: float | None
    fused_score: float
    dense_rank: int | None
    sparse_rank: int | None
    rerank_score: float | None = None

    @property
    def citation_label(self) -> str:
        return f"{self.act.label}, {self.section_ref}"


# --- Проєкт документа -------------------------------------------------------------

class ProvisionKind(StrEnum):
    TITLE = "title"            # заголовок «Про …»
    PREAMBLE = "preamble"      # «Відповідно до …, з метою …»
    ITEM = "item"              # пункт розпорядчої частини
    BASIS = "basis"            # «Підстава: …»
    SIGNATURE = "signature"    # посада підписанта


@dataclass(frozen=True, slots=True)
class Provision:
    """Пункт проєкту, який перевіряється окремо. Текст — уже з мітками ПД."""

    id: str                    # «P1»
    kind: ProvisionKind
    label: str                 # «п. 2 розпорядчої частини»
    text: str


@dataclass(frozen=True, slots=True)
class DraftContext:
    """Що відомо про проєкт загалом (передається в кожен промпт для контексту)."""

    title: str | None
    kind_label: str            # «наказ», «розпорядження»
    signer_position: str | None
    preamble: str | None


class Coverage(StrEnum):
    STRONG = "strong"          # знайдено релевантні норми
    WEAK = "weak"              # знайдено, але слабкий збіг
    NONE = "none"              # нічого не знайдено


@dataclass(slots=True)
class CandidateFinding:
    """Зауваження до перевірки цитат і верифікатора."""

    provision_id: str
    category: FindingCategory
    severity: FindingSeverity
    title: str
    explanation: str
    recommendation: str | None
    draft_quote: str           # з мітками ПД
    norm_quote: str
    clause: RetrievedClause
    llm_confidence: float
    rank_in_retrieval: int     # 1 — найрелевантніша норма для пункту
    origin: str = "llm"        # llm | reference_check
    extra_citations: list[RetrievedClause] = field(default_factory=list)
    verifier_verdict: str | None = None
    verifier_reason: str | None = None
    verifier_confidence: float | None = None
