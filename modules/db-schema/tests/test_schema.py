"""Перевірка бізнес-інваріантів схеми: життєвий цикл документа, незмінність,
Human-in-the-loop, КЕП, комплаєнс-логи та цілісність журналу аудиту."""

from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from collections.abc import Iterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, selectinload

from sed_db.enums import (
    AiRunStatus,
    AiTaskType,
    ComplianceVerdict,
    DocumentStatus,
    FindingCategory,
    FindingSeverity,
    FindingStatus,
    HumanReviewStatus,
    ModelProvider,
    RegulatoryLevel,
    RegulatoryStatus,
    RouteOrigin,
    RouteStatus,
    SignatureFormat,
    SignatureProvider,
    SignatureValidation,
    StepKind,
    StepStatus,
    UserRole,
    VersionSource,
)
from sed_db.schemas import AiAnalysisAuditRead, UserRead
from sed_db.models import (
    AiExtractedField,
    AiModel,
    ApprovalStepHistory,
    AuditLog,
    DocumentStatusHistory,
    User,
    UserRoleAssignment,
    AiRun,
    ApprovalRoute,
    ApprovalStep,
    ComplianceCheck,
    ComplianceCitation,
    ComplianceFinding,
    Document,
    DocumentVersion,
    RegulatoryChunk,
    RegulatorySource,
    Signature,
    StaffAssignment,
)

from .conftest import act_as

NOW = datetime.now(timezone.utc)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@contextmanager
def db_error(session: Session, sqlstate: str) -> Iterator[None]:
    """Очікувана помилка БД із заданим SQLSTATE; транзакція тесту лишається живою."""
    with pytest.raises(DBAPIError) as exc:
        with session.begin_nested():
            yield
            session.flush()
    assert exc.value.orig.sqlstate == sqlstate, exc.value


def make_document(session: Session, w: dict, content: bytes = b"%PDF-1.7 order") -> tuple[Document, DocumentVersion]:
    doc = Document(document_type_id=w["order_type"].id, direction=w["direction"],
                   access_level=w["order_type"].default_access_level,
                   title="Про організацію освітнього процесу у 2026/2027 н.р.",
                   author_id=w["author"].id, org_unit_id=w["dept"].id)
    session.add(doc)
    session.flush()
    ver = DocumentVersion(document_id=doc.id, version_number=1, source=VersionSource.EDITOR,
                          storage_bucket="sed-documents", storage_key=f"{doc.id}/v1.pdf",
                          file_name="nakaz.pdf", mime_type="application/pdf",
                          size_bytes=len(content), content_sha256=sha(content),
                          created_by_id=w["author"].id)
    session.add(ver)
    session.flush()
    doc.current_version_id = ver.id
    session.flush()
    return doc, ver


def make_signature(ver: DocumentVersion, signer_id: uuid.UUID, step_id: uuid.UUID | None,
                   content_hash: str | None = None) -> Signature:
    return Signature(
        document_version_id=ver.id, signer_id=signer_id, approval_step_id=step_id,
        signature_format=SignatureFormat.CADES_X_LONG, provider=SignatureProvider.DIIA_SIGNATURE,
        storage_bucket="sed-signatures", storage_key=f"{ver.id}/{signer_id}.p7s",
        signed_content_sha256=content_hash or ver.content_sha256,
        cert_serial="58E2D9E7F900307B04000000", cert_issuer="КНЕДП ДПС", cert_subject_cn="Іван Ректоренко",
        cert_valid_from=NOW - timedelta(days=100), cert_valid_to=NOW + timedelta(days=265), signed_at=NOW,
    )


def make_llm_run(session: Session, ver: DocumentVersion, task: AiTaskType, confidence: str,
                 review: HumanReviewStatus = HumanReviewStatus.NOT_REQUIRED) -> AiRun:
    model = AiModel(name=f"mistral-uk-{uuid.uuid4().hex[:6]}", version="1.0",
                    provider=ModelProvider.VLLM, task_type=task)
    session.add(model)
    session.flush()
    run = AiRun(task_type=task, status=AiRunStatus.SUCCEEDED, document_id=ver.document_id,
                document_version_id=ver.id, model_id=model.id, pii_masked=True,
                confidence=Decimal(confidence), human_review_status=review,
                started_at=NOW, finished_at=NOW)
    session.add(run)
    session.flush()
    return run


# --------------------------------------------------------------------------- tests

def test_full_lifecycle_draft_to_registered(session: Session, world: dict) -> None:
    w = world
    act_as(session, w["author"].id)
    doc, ver = make_document(session, w)

    route = ApprovalRoute(document_id=doc.id, document_version_id=ver.id, origin=RouteOrigin.MANUAL,
                          status=RouteStatus.ACTIVE, started_at=NOW, current_stage=1,
                          initiated_by_id=w["author"].id)
    session.add(route)
    session.flush()
    visa = ApprovalStep(route_id=route.id, stage_no=1, ordinal=1, step_kind=StepKind.VISA,
                        assignee_id=w["head"].id, status=StepStatus.ACTIVE, activated_at=NOW,
                        due_at=NOW + timedelta(days=2))
    sign = ApprovalStep(route_id=route.id, stage_no=2, ordinal=1, step_kind=StepKind.SIGNING,
                        assignee_id=w["rector"].id, requires_kep=True)
    session.add_all([visa, sign])
    doc.status = DocumentStatus.ON_APPROVAL
    session.flush()

    session.refresh(ver)
    assert ver.is_locked, "активація маршруту блокує версію"

    act_as(session, w["head"].id)
    visa.status, visa.decided_at = StepStatus.APPROVED, NOW
    sign.status, sign.activated_at = StepStatus.ACTIVE, NOW
    session.flush()

    act_as(session, w["rector"].id)
    sig = make_signature(ver, w["rector"].id, sign.id)
    session.add(sig)
    session.flush()
    # Валідація підпису (OCSP/CRL) — окремий крок; лише поля validation_* можна змінювати.
    sig.validation_status, sig.validated_at = SignatureValidation.VALID, NOW
    session.flush()  # підпис має бути валідованим ДО зміни статусу документа
    sign.status, sign.decided_at = StepStatus.APPROVED, NOW
    route.status, route.completed_at = RouteStatus.COMPLETED, NOW
    doc.status = DocumentStatus.SIGNED
    session.flush()

    number = session.execute(
        text("SELECT sed_register_document(:d, :j)"), {"d": doc.id, "j": w["journal"].id}
    ).scalar_one()
    assert number == "1-од"
    session.expire(doc)
    assert doc.status is DocumentStatus.REGISTERED and doc.registration_number == "1-од"

    loaded = session.scalars(
        select(Document).options(selectinload(Document.versions)).where(Document.id == doc.id)
    ).one()
    assert [v.version_number for v in loaded.versions] == [1]

    history = session.execute(
        text("SELECT entity_table, action FROM audit_logs WHERE document_id = :d ORDER BY chain_seq"),
        {"d": doc.id},
    ).all()
    assert ("signatures", "insert") in history and ("documents", "update") in history

    statuses = session.scalars(
        select(DocumentStatusHistory.to_status).where(DocumentStatusHistory.document_id == doc.id)
        .order_by(DocumentStatusHistory.id)
    ).all()
    assert statuses == [DocumentStatus.DRAFT, DocumentStatus.ON_APPROVAL,
                        DocumentStatus.SIGNED, DocumentStatus.REGISTERED]
    steps = session.execute(
        select(ApprovalStepHistory.step_id, ApprovalStepHistory.to_status, ApprovalStepHistory.changed_by_id)
        .where(ApprovalStepHistory.route_id == route.id).order_by(ApprovalStepHistory.id)
    ).all()
    assert [(st, by) for sid, st, by in steps if sid == visa.id] == [
        (StepStatus.ACTIVE, w["author"].id), (StepStatus.APPROVED, w["head"].id)]
    assert session.execute(text("SELECT * FROM sed_audit_verify()")).all() == []


def test_locked_version_content_is_immutable(session: Session, world: dict) -> None:
    doc, ver = make_document(session, world)
    with db_error(session, "23001"):
        ver.storage_key = "tampered.pdf"
    session.expire_all()
    ver.is_locked, ver.locked_at = True, NOW
    session.flush()
    with db_error(session, "23001"):
        session.delete(ver)


def test_signature_must_match_version_hash(session: Session, world: dict) -> None:
    _, ver = make_document(session, world)
    with db_error(session, "23514"):
        session.add(make_signature(ver, world["rector"].id, None, content_hash=sha(b"other file")))


def test_signature_row_is_append_only(session: Session, world: dict) -> None:
    _, ver = make_document(session, world)
    sig = make_signature(ver, world["rector"].id, None)
    session.add(sig)
    session.flush()
    with db_error(session, "23001"):
        sig.cert_subject_cn = "Хтось інший"
    session.expire_all()
    with db_error(session, "23001"):
        session.delete(sig)


def test_document_cannot_be_signed_without_valid_kep(session: Session, world: dict) -> None:
    doc, ver = make_document(session, world)
    session.add(make_signature(ver, world["rector"].id, None))  # validation_status = pending
    session.flush()
    with db_error(session, "23514"):
        doc.status = DocumentStatus.SIGNED


def test_ai_actor_cannot_make_legal_decisions(session: Session, world: dict) -> None:
    doc, ver = make_document(session, world)
    act_as(session, None, "ai_service")
    doc.status = DocumentStatus.AI_REVIEW   # дозволено: службовий статус перевірки
    session.flush()
    with db_error(session, "42501"):
        doc.status = DocumentStatus.APPROVED
    session.expire_all()
    with db_error(session, "42501"):
        session.add(make_signature(ver, world["rector"].id, None))


def test_low_confidence_requires_human_review(session: Session, world: dict) -> None:
    _, ver = make_document(session, world)
    with db_error(session, "23514"):
        make_llm_run(session, ver, AiTaskType.NER_EXTRACTION, "0.8400")
    session.expire_all()
    run = make_llm_run(session, ver, AiTaskType.NER_EXTRACTION, "0.8400", HumanReviewStatus.PENDING)
    session.refresh(run)
    assert run.requires_human_review is True

    high = make_llm_run(session, ver, AiTaskType.CLASSIFICATION, "0.9700")
    session.refresh(high)
    assert high.requires_human_review is False

    with db_error(session, "23514"):  # поріг не можна знизити
        high.confidence_threshold = Decimal("0.5")
    session.expire_all()

    # Ручна валідація вимагає reviewer; ШІ не може «сам себе» підтвердити.
    act_as(session, None, "ai_service")
    with db_error(session, "42501"):
        run.human_review_status, run.reviewed_by_id, run.reviewed_at = (
            HumanReviewStatus.CONFIRMED, world["operator"].id, NOW)
    session.expire_all()
    act_as(session, world["operator"].id)
    run.human_review_status, run.reviewed_by_id, run.reviewed_at = (
        HumanReviewStatus.CONFIRMED, world["operator"].id, NOW)
    session.flush()


def test_llm_input_must_be_pii_masked(session: Session, world: dict) -> None:
    _, ver = make_document(session, world)
    run = make_llm_run(session, ver, AiTaskType.COMPLIANCE_CHECK, "0.9")
    with db_error(session, "23514"):
        run.pii_masked = False


def test_compliance_log_with_citations(session: Session, world: dict) -> None:
    _, ver = make_document(session, world)
    run = make_llm_run(session, ver, AiTaskType.COMPLIANCE_CHECK, "0.9100")
    statute = RegulatorySource(level=RegulatoryLevel.UNIVERSITY, issuer="Вчена рада ПУЕТ",
                               title="Статут ПУЕТ", status=RegulatoryStatus.IN_FORCE,
                               effective_from=date(2023, 1, 1))
    session.add(statute)
    session.flush()
    chunk = RegulatoryChunk(source_id=statute.id, index_revision=1, chunk_no=42,
                            section_ref="розд. 5, п. 5.3",
                            content="Ректор видає накази, обов'язкові для виконання всіма учасниками освітнього процесу.",
                            embedding=[0.01] * 1024, embedding_model_id=run.model_id)
    session.add(chunk)
    session.flush()

    check = ComplianceCheck(ai_run_id=run.id, document_version_id=ver.id,
                            verdict=ComplianceVerdict.ISSUES_FOUND, regulations_as_of=date.today(),
                            retrieval_config={"dense_k": 20, "bm25_k": 20, "fusion": "rrf", "rrf_k": 60})
    session.add(check)
    session.flush()
    finding = ComplianceFinding(check_id=check.id, ordinal=1, severity=FindingSeverity.MAJOR,
                                category=FindingCategory.COMPETENCE_VIOLATION,
                                title="Наказ підписує особа без повноважень",
                                explanation="Згідно зі Статутом накази видає ректор.",
                                confidence=Decimal("0.8100"), char_start=0, char_end=40)
    session.add(finding)
    session.flush()
    session.add(ComplianceCitation(finding_id=finding.id, chunk_id=chunk.id, rank=1,
                                   quoted_text="Ректор видає накази", quote_verified=True,
                                   source_status_snapshot=RegulatoryStatus.IN_FORCE,
                                   dense_score=0.82, sparse_score=11.4, fused_score=0.031))
    session.flush()
    session.refresh(finding)
    assert finding.requires_human_review is True

    # Найближчий фрагмент за косинусною відстанню (HNSW).
    nearest = session.execute(
        select(RegulatoryChunk.id).order_by(RegulatoryChunk.embedding.cosine_distance([0.01] * 1024)).limit(1)
    ).scalar_one()
    assert nearest == chunk.id

    with db_error(session, "23514"):  # відхилення без обґрунтування
        finding.status, finding.resolved_by_id, finding.resolved_at = (
            FindingStatus.DISMISSED, world["operator"].id, NOW)
    session.expire_all()
    with db_error(session, "23001"):  # лог перевірки незмінний
        check.verdict = ComplianceVerdict.COMPLIANT


def test_one_open_route_per_document(session: Session, world: dict) -> None:
    doc, ver = make_document(session, world)
    session.add(ApprovalRoute(document_id=doc.id, document_version_id=ver.id, origin=RouteOrigin.MANUAL,
                              initiated_by_id=world["author"].id))
    session.flush()
    with db_error(session, "23505"):
        session.add(ApprovalRoute(document_id=doc.id, document_version_id=ver.id,
                                  origin=RouteOrigin.MANUAL, initiated_by_id=world["author"].id))


def test_route_version_must_belong_to_document(session: Session, world: dict) -> None:
    doc_a, _ = make_document(session, world)
    _, ver_b = make_document(session, world, b"%PDF another")
    with db_error(session, "23503"):
        session.add(ApprovalRoute(document_id=doc_a.id, document_version_id=ver_b.id,
                                  origin=RouteOrigin.MANUAL, initiated_by_id=world["author"].id))


def test_rejection_requires_comment(session: Session, world: dict) -> None:
    doc, ver = make_document(session, world)
    route = ApprovalRoute(document_id=doc.id, document_version_id=ver.id, origin=RouteOrigin.MANUAL,
                          initiated_by_id=world["author"].id)
    session.add(route)
    session.flush()
    with db_error(session, "23514"):
        session.add(ApprovalStep(route_id=route.id, stage_no=1, ordinal=1, step_kind=StepKind.VISA,
                                 assignee_id=world["head"].id, status=StepStatus.REJECTED,
                                 activated_at=NOW, decided_at=NOW))


def test_single_unit_head_at_a_time(session: Session, world: dict) -> None:
    w = world
    session.add(StaffAssignment(user_id=w["head"].id, org_unit_id=w["dept"].id, position_code="ZAV_KAF",
                                position_title_uk="Завідувач кафедри", is_head=True, valid_from=date(2024, 9, 1)))
    session.flush()
    with db_error(session, "23P01"):
        session.add(StaffAssignment(user_id=w["author"].id, org_unit_id=w["dept"].id, position_code="ZAV_KAF",
                                    position_title_uk="Завідувач кафедри", is_head=True,
                                    valid_from=date(2025, 1, 1)))


def test_registration_numbers_have_no_gaps(session: Session, world: dict) -> None:
    w = world
    docs = []
    for i in range(2):
        doc, ver = make_document(session, w, f"%PDF {i}".encode())
        sig = make_signature(ver, w["rector"].id, None)
        session.add(sig)
        session.flush()
        sig.validation_status, sig.validated_at = SignatureValidation.VALID, NOW
        session.flush()
        doc.status = DocumentStatus.SIGNED
        session.flush()
        docs.append(doc)

    reg = text("SELECT sed_register_document(:d, :j)")
    with pytest.raises(DBAPIError):
        with session.begin_nested():  # реєстрація зірвалась → номер не «згорів»
            session.execute(reg, {"d": docs[0].id, "j": w["journal"].id})
            session.execute(text("SELECT 1/0"))
    assert session.execute(reg, {"d": docs[0].id, "j": w["journal"].id}).scalar_one() == "1-од"
    assert session.execute(reg, {"d": docs[1].id, "j": w["journal"].id}).scalar_one() == "2-од"
    with db_error(session, "23514"):  # повторна реєстрація
        session.execute(reg, {"d": docs[1].id, "j": w["journal"].id})


def test_audit_log_is_tamper_evident(session: Session, world: dict) -> None:
    make_document(session, world)
    with db_error(session, "23001"):
        session.execute(text("UPDATE audit_logs SET action = 'x'"))
    with db_error(session, "23001"):
        session.execute(text("DELETE FROM audit_logs"))

    # Навіть DBA, що вимкнув тригер, не приховає підробку: ланцюг хешів розривається.
    seq = session.execute(text("SELECT max(chain_seq) - 1 FROM audit_logs")).scalar_one()
    session.execute(text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only"))
    session.execute(text("UPDATE audit_logs SET changes = '{}'::jsonb WHERE chain_seq = :s"), {"s": seq})
    session.execute(text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only"))
    broken = session.execute(text("SELECT broken_seq, reason FROM sed_audit_verify()")).all()
    assert broken == [(seq, "record_hash mismatch")]


def test_ai_worker_db_role_is_least_privilege(session: Session, world: dict) -> None:
    """roles.sql: навіть без змінної sed.actor_type воркер ШІ не підпише й не погодить."""
    from pathlib import Path

    exists = session.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'sed_ai_worker'")).first()
    if not exists:
        sql = (Path(__file__).resolve().parents[1] / "roles.sql").read_text()
        session.execute(text(sql))
    doc, ver = make_document(session, world)
    session.execute(text("SET LOCAL ROLE sed_ai_worker"))
    with db_error(session, "42501"):
        session.execute(text("UPDATE documents SET status = 'approved' WHERE id = :d"), {"d": doc.id})
    with db_error(session, "42501"):
        session.execute(text("DELETE FROM documents WHERE id = :d"), {"d": doc.id})
    session.execute(text("UPDATE documents SET status = 'ai_review' WHERE id = :d"), {"d": doc.id})
    session.execute(text("RESET ROLE"))
    session.execute(text("SET LOCAL ROLE sed_app"))
    with db_error(session, "42501"):  # прямий запис у журнал заборонено
        session.execute(text("INSERT INTO audit_logs (actor_type, action, entity_table) VALUES ('user','x','y')"))
    act_as(session, world["operator"].id)
    session.execute(text("SELECT sed_audit_event('document.viewed', 'documents', :d, :d)"), {"d": doc.id})
    session.execute(text("RESET ROLE"))
    assert session.execute(text("SELECT * FROM sed_audit_verify()")).all() == []



# --------------------------------------------------------------- roles & AI audit

def test_user_roles_scope_and_uniqueness(session: Session, world: dict) -> None:
    w = world
    session.add_all([
        UserRoleAssignment(user_id=w["head"].id, role=UserRole.LECTURER, org_unit_id=w["dept"].id,
                           valid_from=date(2020, 9, 1)),
        UserRoleAssignment(user_id=w["head"].id, role=UserRole.DEPARTMENT_HEAD, org_unit_id=w["dept"].id,
                           valid_from=date(2024, 9, 1)),
        UserRoleAssignment(user_id=w["rector"].id, role=UserRole.RECTOR, valid_from=date(2022, 1, 1)),
    ])
    session.flush()

    with db_error(session, "23514"):  # викладач без кафедри
        session.add(UserRoleAssignment(user_id=w["author"].id, role=UserRole.LECTURER, valid_from=date(2024, 1, 1)))
    with db_error(session, "23P01"):  # другий завідувач тієї ж кафедри
        session.add(UserRoleAssignment(user_id=w["author"].id, role=UserRole.DEPARTMENT_HEAD,
                                       org_unit_id=w["dept"].id, valid_from=date(2025, 1, 1)))
    with db_error(session, "23P01"):  # другий ректор
        session.add(UserRoleAssignment(user_id=w["author"].id, role=UserRole.RECTOR, valid_from=date(2025, 1, 1)))
    with db_error(session, "23514"):  # завідувач кафедри, призначений на університет
        session.add(UserRoleAssignment(user_id=w["author"].id, role=UserRole.DEPARTMENT_HEAD,
                                       org_unit_id=w["uni"].id, valid_from=date(2025, 1, 1)))
    # Наступник після закінчення повноважень попереднього — дозволено.
    session.execute(text("UPDATE user_roles SET valid_to = '2029-12-31' WHERE role = 'rector' AND valid_to IS NULL "
                         "AND user_id = :u"), {"u": w["rector"].id})
    session.add(UserRoleAssignment(user_id=w["author"].id, role=UserRole.RECTOR, valid_from=date(2030, 1, 1)))
    session.flush()

    head = session.scalars(select(User).options(selectinload(User.roles)).where(User.id == w["head"].id)).one()
    dto = UserRead.model_validate(head)
    assert {r.role_label_uk for r in dto.roles} == {"Викладач", "Завідувач кафедри"}
    assert dto.signature_name == "Андрій КОВАЛЬ"


def test_ai_analysis_written_to_audit_logs(session: Session, world: dict) -> None:
    w = world
    w["order_type"].requisites_schema = {
        "type": "object",
        "properties": {"order_number": {"type": "string"},
                       "signer_full_name": {"type": "string", "x-pii": True}},
    }
    _, ver = make_document(session, w)
    run = make_llm_run(session, ver, AiTaskType.COMPLIANCE_CHECK, "0.8200", HumanReviewStatus.PENDING)
    statute = RegulatorySource(level=RegulatoryLevel.UNIVERSITY, issuer="Вчена рада ПУЕТ",
                               title="Статут ПУЕТ", status=RegulatoryStatus.IN_FORCE)
    session.add(statute)
    session.flush()
    chunk = RegulatoryChunk(source_id=statute.id, index_revision=1, chunk_no=1, section_ref="п. 5.3",
                            content="Ректор видає накази.")
    session.add(chunk)
    session.flush()
    check = ComplianceCheck(ai_run_id=run.id, document_version_id=ver.id,
                            verdict=ComplianceVerdict.ISSUES_FOUND, regulations_as_of=date.today())
    session.add(check)
    session.flush()
    finding = ComplianceFinding(check_id=check.id, ordinal=1, severity=FindingSeverity.CRITICAL,
                                category=FindingCategory.COMPETENCE_VIOLATION,
                                title="Підписант без повноважень", explanation="…",
                                confidence=Decimal("0.9000"))
    session.add(finding)
    session.flush()
    session.add(ComplianceCitation(finding_id=finding.id, chunk_id=chunk.id, rank=1,
                                   quoted_text="Ректор видає накази", quote_verified=True,
                                   source_status_snapshot=RegulatoryStatus.IN_FORCE))
    session.add_all([
        AiExtractedField(ai_run_id=run.id, document_version_id=ver.id, field_key="order_number",
                         extracted_value="125-од", confidence=Decimal("0.9900")),
        AiExtractedField(ai_run_id=run.id, document_version_id=ver.id, field_key="signer_full_name",
                         extracted_value="Іван Ректоренко", confidence=Decimal("0.7000"),
                         human_review_status=HumanReviewStatus.PENDING),
    ])
    session.flush()

    # Відкладений тригер спрацьовує на COMMIT; у тесті форсуємо його негайно.
    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    row = session.scalars(select(AuditLog).where(AuditLog.ai_run_id == run.id)).one()
    dto = AiAnalysisAuditRead.model_validate(row)
    assert dto.confidence_score == Decimal("0.8200") and dto.requires_human_review
    assert dto.ai_verdict == "issues_found" and dto.ai_model and dto.ai_model.startswith("mistral-uk-")
    assert dto.violations[0].category is FindingCategory.COMPETENCE_VIOLATION
    assert dto.violations[0].citations[0].source == "Статут ПУЕТ"
    assert dto.extracted_metadata["order_number"].value == "125-од"
    pii = dto.extracted_metadata["signer_full_name"]
    assert pii.masked and pii.value is None and pii.requires_human_review
    assert "Ректоренко" not in str(row.extracted_metadata)
    assert session.execute(text("SELECT * FROM sed_audit_verify()")).all() == []

    # Підробка confidence у журналі виявляється перевіркою ланцюга.
    session.execute(text("ALTER TABLE audit_logs DISABLE TRIGGER trg_audit_logs_append_only"))
    session.execute(text("UPDATE audit_logs SET confidence_score = 0.99 WHERE ai_run_id = :r"), {"r": run.id})
    session.execute(text("ALTER TABLE audit_logs ENABLE TRIGGER trg_audit_logs_append_only"))
    assert session.execute(text("SELECT reason FROM sed_audit_verify()")).scalar_one() == "record_hash mismatch"


def test_failed_ai_run_is_not_logged_as_analysis(session: Session, world: dict) -> None:
    _, ver = make_document(session, world)
    run = make_llm_run(session, ver, AiTaskType.CLASSIFICATION, "0.9")
    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert session.scalars(select(AuditLog).where(AuditLog.ai_run_id == run.id)).one().ai_verdict is None
    model_id = run.model_id
    failed = AiRun(task_type=AiTaskType.CLASSIFICATION, status=AiRunStatus.FAILED, model_id=model_id,
                   document_version_id=ver.id, pii_masked=True, error_message="CUDA OOM")
    session.add(failed)
    session.flush()
    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert session.scalars(select(AuditLog).where(AuditLog.ai_run_id == failed.id)).first() is None
