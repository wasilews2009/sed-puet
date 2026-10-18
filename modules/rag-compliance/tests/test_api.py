"""HTTP-контракт: автентифікація, перевірка тексту, перевірка версії, пошук, адміністрування корпусу."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import date

import httpx
import pytest

from sed_ingest.api.auth import InvalidToken, Principal
from sed_rag.api.app import create_app
from sed_rag.config import Settings
from sed_rag.corpus.sparse import Bm25Encoder
from sed_rag.domain.models import AccessDenied
from sed_rag.ports import CheckRecord, SavedCheck, VersionForCheck
from sed_rag.retrieval.hybrid import HybridRetriever
from sed_rag.service import ComplianceService

from .conftest import AS_OF, CLEAN_ORDER, ORDER_TEXT, SYSTEM_USER, RuleLlm, make_auditor, settings

USER = uuid.uuid4()
ADMIN = uuid.uuid4()
VERSION = uuid.uuid4()


class Tokens:
    def verify(self, token: str) -> Principal:
        if token == "user":
            return Principal(sub=USER)
        if token == "admin":
            return Principal(sub=ADMIN, roles=frozenset({"sed-regulations-admin"}))
        raise InvalidToken("bad")


class FakeComplianceRepo:
    def __init__(self) -> None:
        self.saved: list[CheckRecord] = []

    async def load_version(self, keycloak_sub: uuid.UUID, version_id: uuid.UUID) -> VersionForCheck:
        if keycloak_sub != USER:
            raise AccessDenied("Немає доступу до цього документа")
        return VersionForCheck(user_id=uuid.uuid4(), document_id=uuid.uuid4(), version_id=version_id,
                               text=ORDER_TEXT, title="Про призначення", document_type_code="ORDER_MAIN",
                               document_date=AS_OF)

    async def save_check(self, record: CheckRecord) -> SavedCheck:
        self.saved.append(record)
        return SavedCheck(ai_run_id=uuid.uuid4(), check_id=uuid.uuid4())


@pytest.fixture()
async def client(corpus, ingestor, index, embedder) -> AsyncIterator[tuple[httpx.AsyncClient, FakeComplianceRepo]]:  # type: ignore[no-untyped-def]
    repo = FakeComplianceRepo()
    s = settings()
    aud = make_auditor(index, embedder, RuleLlm(), s)
    service = ComplianceService(aud, HybridRetriever(index, embedder, Bm25Encoder(), s.retrieval), repo)

    async def resolve(sub: uuid.UUID) -> uuid.UUID:
        return SYSTEM_USER

    app = create_app(Settings(), service=service, ingestor=ingestor, resolve_user=resolve, token_verifier=Tokens())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield c, repo


def _h(token: str = "user") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_requires_token(client) -> None:  # type: ignore[no-untyped-def]
    c, _ = client
    assert (await c.post("/api/v1/compliance/check", json={"text": CLEAN_ORDER})).status_code == 401
    r = await c.post("/api/v1/compliance/check", json={"text": CLEAN_ORDER}, headers=_h("bad"))
    assert r.status_code == 401 and r.json()["detail"] == "Недійсний або прострочений токен"


async def test_check_text(client) -> None:  # type: ignore[no-untyped-def]
    c, repo = client
    r = await c.post("/api/v1/compliance/check", json={"text": ORDER_TEXT, "as_of": "2026-10-17"}, headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] in {"issues_found", "non_compliant"} and body["check_id"] is None
    assert body["violated_clauses"][0]["citation_label"] == "Статут ПУЕТ, п. 12.1"
    assert body["confidence_threshold"] == "0.85" and "юридичної сили" in body["disclaimer"]
    assert not repo.saved                                          # ad-hoc перевірка нічого не пише


async def test_check_text_validation(client) -> None:  # type: ignore[no-untyped-def]
    c, _ = client
    assert (await c.post("/api/v1/compliance/check", json={"text": "коротко"}, headers=_h())).status_code == 422
    r = await c.post("/api/v1/compliance/check", json={"text": CLEAN_ORDER, "extra": 1}, headers=_h())
    assert r.status_code == 422


async def test_check_version_persists(client) -> None:  # type: ignore[no-untyped-def]
    c, repo = client
    r = await c.post(f"/api/v1/documents/versions/{VERSION}/compliance-checks", json={},
                     headers={**_h(), "X-Request-ID": "req-1"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ai_run_id"] and body["check_id"] and body["document_version_id"] == str(VERSION)
    rec = repo.saved[0]
    assert rec.request_id == "req-1" and rec.report.regulations_as_of == AS_OF
    assert [p[0] for p in rec.prompts] == ["compliance.assess_provision", "compliance.verify_finding",
                                            "compliance.rewrite_query"]
    assert rec.pii_entities_masked >= 3 and len(rec.input_sha256) == 64


async def test_check_version_access_denied(client) -> None:  # type: ignore[no-untyped-def]
    c, _ = client
    r = await c.post(f"/api/v1/documents/versions/{VERSION}/compliance-checks", headers=_h("admin"))
    assert r.status_code == 403 and r.json()["code"] == "access_denied"


async def test_search(client) -> None:  # type: ignore[no-untyped-def]
    c, _ = client
    r = await c.get("/api/v1/regulations/search", params={"q": "зміни до Статуту затверджуються", "k": 3}, headers=_h())
    assert r.status_code == 200
    hits = r.json()["hits"]
    assert hits[0]["citation_label"] == "Статут ПУЕТ, п. 12.1" and len(hits) <= 3


async def test_corpus_admin_only(client) -> None:  # type: ignore[no-untyped-def]
    c, _ = client
    payload = {"act": {"title": "Положення про тестовий підрозділ", "level": "unit", "issuer": "Вчена рада ПУЕТ",
                       "effective_from": "2026-01-01"},
               "text": "1. ЗАГАЛЬНІ ПОЛОЖЕННЯ\n1.1. Тестовий підрозділ підпорядковується проректору з навчальної роботи."}
    assert (await c.post("/api/v1/regulations", json=payload, headers=_h())).status_code == 403
    r = await c.post("/api/v1/regulations", json=payload, headers=_h("admin"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "created" and r.json()["sections"] == ["п. 1.1"]
    again = await c.post("/api/v1/regulations", json=payload, headers=_h("admin"))
    assert again.json()["status"] == "unchanged"

    sid = r.json()["source_id"]
    bad = {**payload["act"], "status": "repealed"}
    assert (await c.put(f"/api/v1/regulations/{sid}/status", json=bad, headers=_h("admin"))).status_code == 422
    ok = {**bad, "effective_to": str(date(2026, 6, 1))}
    assert (await c.put(f"/api/v1/regulations/{sid}/status", json=ok, headers=_h("admin"))).status_code == 204


async def test_openapi_schema(client) -> None:  # type: ignore[no-untyped-def]
    c, _ = client
    spec = (await c.get("/openapi.json")).json()
    assert "/api/v1/compliance/check" in spec["paths"]
    assert "/api/v1/documents/versions/{version_id}/compliance-checks" in spec["paths"]
    assert "ComplianceReport" in spec["components"]["schemas"]
