"""Спільні фікстури: корпус-зразки, Qdrant у пам'яті, детерміновані фейки ембедингів і LLM.

Qdrant справжній (`AsyncQdrantClient(":memory:")` — той самий клієнт і Query API, що з сервером),
тож фільтри чинності, sparse-вектори з IDF і ревізії перевіряються по-справжньому. Сервер
ембедингів і LLM підмінені: ембединги — хешована проєкція стемів (косинус ≈ лексична близькість),
LLM — «сумлінний аналітик» за правилами, який бере цитати лише з того, що реально бачить у промпті.
"""

from __future__ import annotations

import hashlib
import re
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pytest

sys.dont_write_bytecode = True   # не залишати __pycache__ у спільній папці (зокрема в ../db-schema)
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent / "db-schema"), str(ROOT.parent / "ingest-pipeline")]

from qdrant_client import AsyncQdrantClient  # noqa: E402

from sed_db.enums import RegulatoryLevel, RegulatoryStatus  # noqa: E402
from sed_ingest.domain.models import LlmCompletion, LlmError, LlmIdentity  # noqa: E402
from sed_ingest.pipeline.pii import RegexPseudonymizer  # noqa: E402
from sed_rag.adapters.memory import MemoryCorpusRepository  # noqa: E402
from sed_rag.adapters.qdrant_index import QdrantVectorIndex  # noqa: E402
from sed_rag.audit.graph import ComplianceAuditor  # noqa: E402
from sed_rag.config import RetrievalSettings, Settings  # noqa: E402
from sed_rag.corpus.sparse import Bm25Encoder, tokenize  # noqa: E402
from sed_rag.domain.models import ActMeta  # noqa: E402
from sed_rag.ingestion.service import CorpusIngestor  # noqa: E402
from sed_rag.retrieval.hybrid import HybridRetriever  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
SYSTEM_USER = uuid.UUID("00000000-0000-4000-8000-000000000001")
AS_OF = date(2026, 10, 17)

STATUTE = ActMeta(title="Статут Полтавського університету економіки і торгівлі (зразок)", short_title="Статут ПУЕТ",
                  level=RegulatoryLevel.UNIVERSITY, issuer="Засновник ПУЕТ", effective_from=date(2025, 8, 5),
                  is_sample=True)
LAW = ActMeta(title="Закон України «Про вищу освіту»", short_title="ЗУ «Про вищу освіту»", level=RegulatoryLevel.LAW,
              issuer="Верховна Рада України", doc_number="1556-VII", effective_from=date(2014, 9, 6), is_sample=True)
MON = ActMeta(title="Положення про академічні відпустки та повторне навчання", short_title="Положення МОН про академвідпустки",
              level=RegulatoryLevel.MINISTRY, issuer="МОН України", effective_from=date(2020, 1, 1), is_sample=True)
STYPENDII = ActMeta(title="Положення про стипендіальну комісію Університету", short_title="Положення про стипендіальну комісію",
                    level=RegulatoryLevel.UNIVERSITY, issuer="Вчена рада ПУЕТ", status=RegulatoryStatus.REPEALED,
                    effective_from=date(2019, 9, 1), effective_to=date(2024, 8, 31), is_sample=True)
FUTURE = ActMeta(title="Положення про дуальну освіту", short_title="Положення про дуальну освіту",
                 level=RegulatoryLevel.UNIVERSITY, issuer="Вчена рада ПУЕТ", effective_from=date(2027, 1, 1),
                 is_sample=True)

CORPUS: list[tuple[ActMeta, str]] = [
    (STATUTE, "statut_SAMPLE.txt"),
    (LAW, "zakon_vo_SAMPLE.txt"),
    (MON, "mon_akadem_SAMPLE.txt"),
    (STYPENDII, "stypendii_2019_SAMPLE.txt"),
]

ORDER_TEXT = """ПОЛТАВСЬКИЙ УНІВЕРСИТЕТ ЕКОНОМІКИ І ТОРГІВЛІ
НАКАЗ
17.10.2026 м. Полтава № 312-од
Про призначення завідувача кафедри економіки
Відповідно до Статуту Університету та Положення про стипендіальну комісію Університету, з метою забезпечення роботи кафедри
НАКАЗУЮ:
1. Призначити Петренко Олену Іванівну завідувачем кафедри економіки строком на 7 років без проведення конкурсу.
2. Затвердити зміни до Статуту Університету щодо структури кафедр.
3. Контроль за виконанням наказу покласти на проректора з науково-педагогічної роботи Коваленка Андрія Петровича.
Перший проректор Іван РЕКТОРЕНКО"""

CLEAN_ORDER = """НАКАЗ
17.10.2026 № 313-од
Про надання академічної відпустки
Відповідно до Положення про академічні відпустки та повторне навчання
НАКАЗУЮ:
1. Надати академічну відпустку за медичними показаннями на строк один рік здобувачу Сидоренку Петру Миколайовичу на підставі його заяви.
Ректор Олексій НЕСТУЛЯ"""


# --- ембединги -------------------------------------------------------------------------

class HashEmbedder:
    """Детермінована проєкція «мішка стемів» у 1024 виміри (L2-нормована)."""

    model_name = "test-embed"
    model_version = "1"
    dim = 1024

    def __init__(self) -> None:
        self.calls = 0

    def _vec(self, text: str) -> list[float]:
        v = np.zeros(self.dim)
        for tok in tokenize(text):
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
            v[h % self.dim] += 1.0 if (h >> 20) & 1 else -1.0
        n = np.linalg.norm(v)
        return (v / n if n else v).tolist()

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._vec(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        return self._vec(text)


# --- «LLM» ------------------------------------------------------------------------------

_NORM_BLOCK = re.compile(r"\[N(\d+)\] (.+?) — (.+?)\n.*?<<<НОРМА N\1>>>\n(.*?)\n<<<КІНЕЦЬ НОРМИ N\1>>>", re.DOTALL)


def provision_of(user: str) -> str:
    return user.split("<<<ПУНКТ>>>", 1)[1].split("<<<КІНЕЦЬ ПУНКТУ>>>", 1)[0].strip()


def norms_of(user: str) -> list[tuple[int, str, str, str]]:
    return [(int(m.group(1)), m.group(2), m.group(3), m.group(4)) for m in _NORM_BLOCK.finditer(user)]


def sentence_with(text: str, needle: str) -> str | None:
    for s in re.split(r"(?<=[.;])\s+", text):
        if needle.casefold() in s.casefold():
            return s.strip()
    return None


# Правило: (у пункті, у нормі, категорія, серйозність, заголовок, цитата з пункту, впевненість)
Rule = tuple[str, str, str, str, str, str, float]
RULES: list[Rule] = [
    ("строком на 7 років", "строком на п'ять років", "contradiction", "major",
     "Строк обрання завідувача кафедри перевищує п'ять років і порушено конкурсний порядок",
     "строком на 7 років без проведення конкурсу", 0.93),
    ("Затвердити зміни до Статуту", "затверджуються Засновником", "competence_violation", "critical",
     "Зміни до Статуту затверджує Засновник, а не наказ",
     "Затвердити зміни до Статуту Університету", 0.92),
]


class RuleLlm:
    """Фейк порту `StructuredLlm` з журналом промптів."""

    identity = LlmIdentity("test-llm", "1.0", "vllm")

    def __init__(self, rules: list[Rule] | None = None, *, verify: str | Callable[[str], str] = "confirmed",
                 fail: set[str] | None = None, extra: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self.rules = RULES if rules is None else rules
        self.verify = verify
        self.fail = fail or set()
        self.extra = extra
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    async def complete_json(self, messages: list[dict[str, str]], schema: dict[str, Any], schema_name: str) -> LlmCompletion:
        self.calls.append((schema_name, messages))
        if schema_name in self.fail:
            raise LlmError("Локальна LLM недоступна")
        user = messages[-1]["content"] if messages[-1]["role"] == "user" else messages[1]["content"]
        if schema_name == "provision_assessment":
            content = self._assess(messages[1]["content"])
        elif schema_name == "finding_verification":
            verdict = self.verify(user) if callable(self.verify) else self.verify
            content = {"verdict": verdict, "confidence": 0.9, "reason": "Перевірено за текстом норми."}
        elif schema_name == "rewrite_query":
            ids = re.findall(r"^(P\d+): <<<ПУНКТ>>>", user, flags=re.MULTILINE)
            content = {"queries": [{"provision_id": i, "query": "порядок обрання завідувача кафедри за конкурсом строк"}
                                   for i in ids]}
        else:
            raise AssertionError(schema_name)
        if self.extra:
            content = self.extra(schema_name, content)
        return LlmCompletion(content=content, model="test-llm", tokens_in=500, tokens_out=100, latency_ms=10, attempts=1)

    def _assess(self, user: str) -> dict[str, Any]:
        prov = provision_of(user)
        norms = norms_of(user)
        issues = []
        for in_prov, in_norm, cat, sev, title, dq, conf in self.rules:
            if in_prov.casefold() not in prov.casefold():
                continue
            for n, _label, _ref, text in norms:
                quote = sentence_with(text, in_norm)
                if quote:
                    issues.append({"norm_id": f"N{n}", "category": cat, "severity": sev, "title": title,
                                   "explanation": f"Пункт проєкту передбачає «{dq}», тоді як норма встановлює інше.",
                                   "recommendation": "Привести пункт у відповідність до норми.",
                                   "draft_quote": dq, "norm_quote": quote, "confidence": conf})
                    break
        return {"applicable_norm_ids": [f"N{n}" for n, *_ in norms[:2]], "issues": issues,
                "no_issue_confidence": 0.92}

    def prompts(self, schema_name: str | None = None) -> list[str]:
        return [m["content"] for name, msgs in self.calls if schema_name in (None, name) for m in msgs]


# --- зібраний контур ------------------------------------------------------------------------

def settings(**retrieval: Any) -> Settings:
    r = RetrievalSettings(**{"min_dense_score": 0.15, **retrieval})
    return Settings(retrieval=r, llm_concurrency=2)


@pytest.fixture()
async def qdrant() -> AsyncIterator[AsyncQdrantClient]:
    client = AsyncQdrantClient(location=":memory:")
    yield client
    await client.close()


@pytest.fixture()
def embedder() -> HashEmbedder:
    return HashEmbedder()


@pytest.fixture()
def index(qdrant: AsyncQdrantClient) -> QdrantVectorIndex:
    return QdrantVectorIndex(qdrant, f"test_{uuid.uuid4().hex[:8]}")


@pytest.fixture()
def corpus_repo() -> MemoryCorpusRepository:
    return MemoryCorpusRepository()


@pytest.fixture()
def ingestor(corpus_repo: MemoryCorpusRepository, index: QdrantVectorIndex, embedder: HashEmbedder) -> CorpusIngestor:
    return CorpusIngestor(repository=corpus_repo, index=index, embedder=embedder, bm25=Bm25Encoder())


@pytest.fixture()
async def corpus(ingestor: CorpusIngestor) -> dict[str, uuid.UUID]:
    ids: dict[str, uuid.UUID] = {}
    for act, file in CORPUS:
        rep = await ingestor.ingest(act, (FIXTURES / file).read_text(encoding="utf-8"), actor_user_id=SYSTEM_USER)
        ids[act.label] = rep.source_id
    return ids


def make_auditor(index: QdrantVectorIndex, embedder: HashEmbedder, llm: RuleLlm, s: Settings | None = None) -> ComplianceAuditor:
    s = s or settings()
    retriever = HybridRetriever(index, embedder, Bm25Encoder(), s.retrieval)
    return ComplianceAuditor(retriever=retriever, index=index, llm=llm, masker=RegexPseudonymizer(), settings=s)
