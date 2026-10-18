"""Фікстури: жива БД PostgreSQL 16 (+pgvector) з демо-оргструктурою, застосунок зі справжніми
модулями інжесту (фейкові MinIO/OCR/LLM) і маршрутів (імітаційний КНЕДП), HTTP-клієнт.

    export SED_TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost/sed_app_test
    PYTHONPATH=.:../db-schema:../ingest-pipeline:../rag-compliance:../approval-routing pytest -q

Без SED_TEST_DATABASE_URL інтеграційні тести пропускаються; чисті (політика доступу) йдуть завжди.
"""

from __future__ import annotations

import io
import os
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

sys.dont_write_bytecode = True   # не залишати __pycache__ у спільній папці модулів
ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT.parent
sys.path[:0] = [str(ROOT), *(str(MODULES / m) for m in
                             ("db-schema", "ingest-pipeline", "rag-compliance", "approval-routing"))]

from sed_ingest.domain.models import (  # noqa: E402
    IncomingFile,
    LlmCompletion,
    LlmIdentity,
    OcrResult,
    PageText,
    StoredObject,
    TextSource,
)
from sed_ingest.pipeline.prompts import DOC_CLOSE, DOC_OPEN  # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

ORDER_LINES = [
    "ПОЛТАВСЬКИЙ УНІВЕРСИТЕТ ЕКОНОМІКИ І ТОРГІВЛІ",
    "СЛУЖБОВА ЗАПИСКА",
    "17.10.2026 м. Полтава",
    "Про закупівлю комп'ютерної техніки для лабораторії",
    "Прошу погодити закупівлю 10 комп'ютерів для навчальної лабораторії кафедри.",
    "Відповідальна особа: доцент Мельник Андрій Сергійович.",
    "Завідувач кафедри Наталія КРАВЕЦЬ",
]


def make_pdf(lines: list[str]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVu", FONT))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("DejaVu", 12)
    y = 800
    for line in lines:
        c.drawString(60, y, line)
        y -= 22
    c.save()
    return buf.getvalue()


# --- фейки інфраструктури інжесту ------------------------------------------------------------

class MemoryStorage:
    """MinIO у пам'яті: і для запису (порт інжесту), і для читання (FileReader застосунку)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, file: IncomingFile, metadata: dict[str, str]) -> StoredObject:
        self.objects[key] = file.data
        return StoredObject(bucket="sed-documents", key=key, version_id=None, etag="e")

    async def delete(self, obj: StoredObject) -> None:
        self.objects.pop(obj.key, None)

    def open(self, bucket: str, key: str, version_id: str | None) -> Iterator[bytes]:
        from sed_app.domain.errors import NotFound

        if key not in self.objects:
            raise NotFound("Файл версії відсутній у сховищі")
        data = self.objects[key]
        return iter([data[i:i + 1024] for i in range(0, len(data), 1024)])


class TextLayerOcr:
    """Повертає текст, записаний у файл тестом (без Tesseract)."""

    def __init__(self) -> None:
        self.text_by_sha: dict[str, str] = {}

    def extract(self, file: IncomingFile) -> OcrResult:
        text = self.text_by_sha.get(file.sha256, "\n".join(ORDER_LINES))
        return OcrResult(pages=(PageText(1, text, TextSource.TEXT_LAYER, 1.0),), engine="pdf-text-layer",
                         engine_version="test", page_count=1)


def _line(text: str, needle: str) -> str | None:
    return next((ln.strip() for ln in text.splitlines() if needle in ln), None)


class MemoLlm:
    """Сумлінна «модель»: значення і цитати бере з тексту, який бачить. `down=True` — модель недоступна."""

    identity = LlmIdentity("test-llm", "1.0", "vllm")

    def __init__(self) -> None:
        self.down = False
        self.low_confidence = False

    async def complete_json(self, messages: list[dict[str, str]], schema: dict[str, Any],
                            schema_name: str) -> LlmCompletion:
        from sed_ingest.domain.models import LlmError

        if self.down:
            raise LlmError("LLM недоступна")
        user = [m for m in messages if m["role"] == "user"][0]["content"]
        doc = user.split(DOC_OPEN, 1)[1].split(DOC_CLOSE, 1)[0]
        title = _line(doc, "Про ")
        conf = 0.6 if self.low_confidence else 0.95
        content = {
            "document_type": {"value": "memo", "evidence": "СЛУЖБОВА ЗАПИСКА", "confidence": 0.95},
            "document_date": {"value": "17.10.2026", "evidence": _line(doc, "17.10.2026"), "confidence": 0.95},
            "registration_number": {"value": None, "evidence": None, "confidence": 0.9},
            "author": {"value": {"full_name": None, "position": "Завідувач кафедри", "org_unit": None,
                                 "evidence": _line(doc, "Завідувач кафедри")}, "confidence": conf},
            "title": {"value": title, "evidence": title, "confidence": 0.95},
            "summary": {"value": "Прохання погодити закупівлю комп'ютерів для лабораторії кафедри.",
                        "confidence": 0.9},
            "interested_parties": {"value": [], "confidence": 0.9},
        }
        return LlmCompletion(content=content, model="test-llm", tokens_in=500, tokens_out=100, latency_ms=10,
                             attempts=1)


class FakeCompliance:
    """Замість графа LangGraph: фіксує виклик (сам граф тестує модуль sed_rag)."""

    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def check_version(self, keycloak_sub: uuid.UUID, version_id: uuid.UUID, *, as_of: Any = None,
                            request_id: str | None = None) -> Any:
        self.calls.append((keycloak_sub, version_id))

        class _Report:
            def model_dump(self, mode: str = "json") -> dict[str, Any]:
                return {"verdict": "compliant", "document_version_id": str(version_id)}

        return _Report()


# --- БД і застосунок -------------------------------------------------------------------------

@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("SED_TEST_DATABASE_URL")
    if not url:
        pytest.skip("SED_TEST_DATABASE_URL не задано — інтеграційні тести з PostgreSQL пропущено")
    return url


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[Any]:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    url = make_url(database_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()
    os.environ["SED_DATABASE_URL"] = url.render_as_string(hide_password=False)
    command.upgrade(Config(str(MODULES / "db-schema" / "alembic.ini")), "head")
    eng = create_async_engine(url)
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
async def sessionmaker(engine: Any) -> Any:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sed_app.seeds.demo import load, sync_route_templates
    from sed_db.seeds import SYSTEM_USER_ID

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s, s.begin():
        await s.execute(text("SELECT set_config('sed.actor_user_id', :u, true)"), {"u": str(SYSTEM_USER_ID)})
        await load(s)
        results = await sync_route_templates(s)
        assert all("created" in r for r in results), results
    return sm


@dataclass
class Stack:
    app: Any
    issuer: Any
    storage: MemoryStorage
    ocr: TextLayerOcr
    llm: MemoLlm
    compliance: FakeCompliance
    users: dict[str, dict[str, Any]] = field(default_factory=dict)


@pytest.fixture(scope="session")
async def stack(sessionmaker: Any) -> AsyncIterator[Stack]:
    from sqlalchemy import select

    from sed_app.auth.tokens import DevTokenIssuer
    from sed_app.config import Settings
    from sed_app.main import Components, create_app
    from sed_app.seeds.demo import USERS
    from sed_db.models import User
    from sed_ingest.adapters.sql_repository import SqlIngestRepository
    from sed_ingest.config import Settings as IngestSettings
    from sed_ingest.pipeline.extraction import RequisitesExtractor
    from sed_ingest.pipeline.orchestrator import IngestPipeline
    from sed_ingest.pipeline.pii import RegexPseudonymizer
    from sed_routing.adapters.signature_store import InMemorySignatureStore
    from sed_routing.api.app import MockSigningDesk
    from sed_routing.config import Settings as RoutingSettings
    from sed_routing.domain.templates import TemplateCatalog
    from sed_routing.kep.mock import MockKnedp
    from sed_routing.kep.models import PemTrustStore, StaticCertificateBinding, TaxIdHasher
    from sed_routing.kep.verifier import CadesVerifier
    from sed_routing.service import ApprovalService

    os.environ["SED_ROUTING_KEP_ALLOW_MOCK"] = "true"
    os.environ["SED_ROUTING_ENVIRONMENT"] = "test"
    settings = Settings(environment="development", auth_mode="dev")
    issuer = DevTokenIssuer(settings.dev_token_issuer, settings.oidc_audience, 3600)
    storage, ocr, llm, compliance = MemoryStorage(), TextLayerOcr(), MemoLlm(), FakeCompliance()
    ing = IngestSettings()
    pipeline = IngestPipeline(
        storage=storage, ocr=ocr, extractor=RequisitesExtractor(llm, RegexPseudonymizer(), 24_000),
        repository=SqlIngestRepository(sessionmaker, ing.document_type_codes, ing.fallback_document_type_code),
        llm_identity=llm.identity,
    )
    knedp, binding, hasher = MockKnedp(), StaticCertificateBinding(), TaxIdHasher(b"x" * 32)
    verifier = CadesVerifier(PemTrustStore.from_pem(knedp.ca_pem), knedp, binding, hasher, allow_mock=True)
    routing = ApprovalService(sessionmaker, TemplateCatalog.from_dir(RoutingSettings().templates_dir), verifier,
                              InMemorySignatureStore())
    app = create_app(settings, components=Components(
        sessionmaker=sessionmaker, token_verifier=issuer.verifier(), dev_issuer=issuer, files=storage,
        ingest_pipeline=pipeline, compliance_service=compliance, routing_service=routing,
        mock_desk=MockSigningDesk(knedp, binding, hasher, sessionmaker),
    ))
    st = Stack(app=app, issuer=issuer, storage=storage, ocr=ocr, llm=llm, compliance=compliance)
    async with sessionmaker() as s:
        for d in USERS:
            u = await s.scalar(select(User).where(User.email == d.email))
            assert u is not None
            st.users[d.login] = {"id": u.id, "sub": u.keycloak_sub, "roles": [r[0].value for r in d.roles],
                                 "unit": u.primary_org_unit_id}
    async with app.router.lifespan_context(app):
        yield st


def token(st: Stack, login: str, roles: list[str] | None = None) -> dict[str, str]:
    u = st.users[login]
    tok, _ = st.issuer.issue(sub=u["sub"], username=login, email=f"{login}@demo.puet.local", given_name="T",
                             family_name="T", roles=u["roles"] if roles is None else roles)
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="session")
async def client(stack: Stack) -> AsyncIterator[Any]:
    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=stack.app), base_url="http://sed.test") as c:
        yield c


# Цифри в маркері можуть скластися в «РНОКПП» → детектор ПД підвищить гриф і тест доступу стане випадковим.
_NO_DIGITS = str.maketrans("0123456789", "ghijklmnop")


async def upload(client: Any, st: Stack, login: str, lines: list[str] | None = None, **form: str) -> dict[str, Any]:
    """Завантажити документ через API інжесту від імені користувача; повертає IngestResponse."""
    lines = lines or [*ORDER_LINES, f"Ідентифікатор тесту {uuid.uuid4().hex.translate(_NO_DIGITS)}"]
    data = make_pdf(lines)
    import hashlib

    st.ocr.text_by_sha[hashlib.sha256(data).hexdigest()] = "\n".join(lines)
    unit = form.pop("org_unit_id", None) or str(st.users[login]["unit"])
    r = await client.post("/api/v1/documents/ingest", headers=token(st, login),
                          files={"file": ("zapyska.pdf", data, "application/pdf")},
                          data={"org_unit_id": unit, **form})
    assert r.status_code == 201, r.text
    return r.json()
