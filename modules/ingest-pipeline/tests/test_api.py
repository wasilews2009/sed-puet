"""Ендпоінт POST /api/v1/documents/ingest: JWT Keycloak, валідація форми, компенсація MinIO."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from sed_ingest.adapters.ocr_engine import TesseractOcrEngine
from sed_ingest.api.app import create_app
from sed_ingest.api.auth import KeycloakJwtVerifier
from sed_ingest.config import Settings
from sed_ingest.domain.models import LlmError
from sed_ingest.pipeline.extraction import RequisitesExtractor
from sed_ingest.pipeline.orchestrator import IngestPipeline
from sed_ingest.pipeline.pii import RegexPseudonymizer

from .conftest import FakeRepository, FakeStorage, ScriptedLlm, order_responder

ISSUER = "https://sso.test/realms/puet"
AUDIENCE = "sed-api"
UNIT = uuid.uuid4()
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key: Any = KEY, **overrides: Any) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": str(uuid.uuid4()), "iat": now, "exp": now + 300,
              "preferred_username": "operator", "realm_access": {"roles": ["staff"]}, **overrides}
    return jwt.encode(claims, key, algorithm="RS256")


class Env:
    def __init__(self, ocr: TesseractOcrEngine, llm: ScriptedLlm) -> None:
        self.storage = FakeStorage()
        self.repo = FakeRepository(allowed_units={UNIT})
        self.llm = llm
        pipeline = IngestPipeline(self.storage, ocr, RequisitesExtractor(llm, RegexPseudonymizer(), 24_000),
                                  self.repo, llm.identity)
        verifier = KeycloakJwtVerifier(ISSUER, AUDIENCE, key_resolver=lambda _: KEY.public_key())
        self.client = TestClient(create_app(Settings(max_file_bytes=2_000_000), pipeline=pipeline,
                                            token_verifier=verifier))

    def post(self, data: bytes, name: str = "nakaz.pdf", *, auth: str | None = None,
             form: dict[str, str] | None = None) -> Any:
        headers = {"Authorization": f"Bearer {auth or token()}", "X-Request-ID": "req-123"}
        return self.client.post("/api/v1/documents/ingest", headers=headers,
                                files={"file": (name, data, "application/octet-stream")},
                                data={"org_unit_id": str(UNIT), **(form or {})})


@pytest.fixture()
def env(ocr_engine: TesseractOcrEngine) -> Iterator[Env]:
    e = Env(ocr_engine, ScriptedLlm(order_responder))
    with e.client:
        yield e


def test_pdf_ingest_returns_validated_requisites(env: Env, order_pdf: bytes) -> None:
    r = env.post(order_pdf)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "processed" and body["requires_human_review"] is False
    assert body["confidence_threshold"] == "0.85" and float(body["confidence"]) >= 0.85
    req = body["requisites"]
    assert req["document_type"]["value"] == "order_main"
    assert req["document_type_label"] == "Наказ з основної діяльності"
    assert req["document_date"]["value"] == "2026-10-17"
    assert req["registration_number"]["value"] == "245-од"
    assert req["author"]["value"]["full_name"] == "Іван РЕКТОРЕНКО"
    assert {p["role"] for p in req["interested_parties"]["value"]} == {"executor", "controller"}
    assert body["ocr"]["used_ocr"] is False and body["ocr"]["pages"][0]["source"] == "text_layer"

    # Оригінал лежить у MinIO під ключем документа, у БД пішов запис з тим самим ключем
    saved = env.repo.saved[0]
    assert body["file"]["storage_key"] in env.storage.objects
    assert body["file"]["storage_key"] == f"documents/{body['document_id']}/v1/{body['version_id']}.pdf"
    assert saved.command.request_id == "req-123" and saved.kind is not None


def test_scan_with_misread_number_goes_to_review(env: Env, order_png: bytes) -> None:
    r = env.post(order_png, "scan.jpg")       # розширення бреше — тип визначено за вмістом
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["file"]["mime_type"] == "image/png" and body["ocr"]["used_ocr"] is True
    if body["requisites"]["registration_number"]["value"] != "245-од":
        assert body["status"] == "needs_review" and body["requires_human_review"] is True
        assert any(reason.startswith("реєстраційний індекс") for reason in body["review_reasons"])


def test_llm_outage_still_registers_document_for_manual_entry(ocr_engine: TesseractOcrEngine,
                                                              order_pdf: bytes) -> None:
    e = Env(ocr_engine, ScriptedLlm(LlmError("Локальна LLM недоступна")))
    with e.client:
        r = e.post(order_pdf)
    body = r.json()
    assert r.status_code == 201 and body["status"] == "manual_entry_required"
    assert body["requisites"] is None and body["confidence"] == "0.0000" and body["requires_human_review"]
    assert e.repo.saved[0].extraction.error == "Локальна LLM недоступна"


def test_sensitive_pii_raises_access_level(env: Env) -> None:
    from .conftest import ORDER_LINES, make_text_pdf
    pdf = make_text_pdf(ORDER_LINES[:7] + ["РНОКПП 3012345678"] + ORDER_LINES[7:])
    body = env.post(pdf, form={"access_level": "internal"}).json()
    assert body["access_level"] == "personal_data"
    assert any("персональні дані" in w for w in body["warnings"])


def test_duplicate_file_is_flagged(env: Env, order_pdf: bytes) -> None:
    import hashlib
    existing = uuid.uuid4()
    env.repo.duplicates[hashlib.sha256(order_pdf).hexdigest()] = existing
    body = env.post(order_pdf).json()
    assert body["duplicate_of"] == str(existing) and any("дублікат" in w for w in body["warnings"])


def test_db_failure_removes_uploaded_object(env: Env, order_pdf: bytes) -> None:
    env.repo.fail_with = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        env.post(order_pdf)
    assert env.storage.objects == {} and len(env.storage.deleted) == 1


def test_rejected_pdf_removes_uploaded_object(env: Env) -> None:
    r = env.post(b"%PDF-1.7 broken garbage")
    assert r.status_code == 422 and r.json()["code"] == "file_rejected"
    assert env.storage.objects == {} and len(env.storage.deleted) == 1


@pytest.mark.parametrize("auth, detail", [
    (None, "Потрібна автентифікація"),
    ("garbage", "Недійсний або прострочений токен"),
    ("wrong-key", "Недійсний або прострочений токен"),
    ("expired", "Недійсний або прострочений токен"),
    ("wrong-aud", "Недійсний або прострочений токен"),
])
def test_authentication_is_enforced(env: Env, order_pdf: bytes, auth: str | None, detail: str) -> None:
    tokens = {"garbage": "garbage", "wrong-key": token(OTHER_KEY),
              "expired": token(exp=int(time.time()) - 10), "wrong-aud": token(aud="other")}
    headers = {"Authorization": f"Bearer {tokens[auth]}"} if auth else {}
    r = env.client.post("/api/v1/documents/ingest", headers=headers,
                        files={"file": ("a.pdf", order_pdf)}, data={"org_unit_id": str(UNIT)})
    assert r.status_code == 401 and r.json()["detail"] == detail
    assert env.storage.objects == {}


def test_foreign_unit_is_forbidden_before_upload(env: Env, order_pdf: bytes) -> None:
    r = env.client.post("/api/v1/documents/ingest", headers={"Authorization": f"Bearer {token()}"},
                        files={"file": ("a.pdf", order_pdf)}, data={"org_unit_id": str(uuid.uuid4())})
    assert r.status_code == 403 and env.storage.objects == {} and not env.storage.deleted


@pytest.mark.parametrize("data, form, code", [
    (b"MZ\x90\x00 exe", {}, 415),
    (b"%PDF-" + b"0" * 2_100_000, {}, 422),
    (b"%PDF-1.7", {"direction": "incoming"}, 422),     # вхідний без кореспондента
    (b"%PDF-1.7", {"access_level": "top_secret"}, 422),
])
def test_bad_requests(env: Env, data: bytes, form: dict[str, str], code: int) -> None:
    assert env.post(data, form=form).status_code == code
    assert env.storage.objects == {}


def test_openapi_documents_endpoint(env: Env) -> None:
    spec = env.client.get("/openapi.json").json()
    op = spec["paths"]["/api/v1/documents/ingest"]["post"]
    assert set(op["responses"]) >= {"201", "401", "403", "415", "422", "503"}
    assert "DocumentRequisites" in spec["components"]["schemas"]
