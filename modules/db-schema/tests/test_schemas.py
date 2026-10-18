"""Юніт-тести Pydantic-схем (без БД)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from sed_db.enums import (
    AiRunStatus,
    AiTaskType,
    ComplianceVerdict,
    DocumentStatus,
    FindingCategory,
    FindingSeverity,
    StepKind,
    StepStatus,
    UserRole,
)
from sed_db.schemas import (
    AiRunCreate,
    AiRunResult,
    ApprovalRouteCreate,
    ComplianceCheckIn,
    FindingIn,
    RouteTemplateCreate,
    StepDecision,
    UserRoleAssignmentCreate,
    check_transition,
)

U = uuid.uuid4


def finding(**kw) -> dict:
    base = dict(severity=FindingSeverity.MAJOR, category=FindingCategory.CONTRADICTION, title="t",
                explanation="e", confidence=Decimal("0.9"),
                citations=[dict(chunk_id=U(), quoted_text="п. 3.2", quote_verified=True)])
    return base | kw


def test_role_scope() -> None:
    UserRoleAssignmentCreate(role=UserRole.VICE_RECTOR, valid_from=date(2025, 1, 1))
    with pytest.raises(ValidationError, match="потребує підрозділу"):
        UserRoleAssignmentCreate(role=UserRole.DEPARTMENT_HEAD, valid_from=date(2025, 1, 1))
    with pytest.raises(ValidationError, match="рівні університету"):
        UserRoleAssignmentCreate(role=UserRole.RECTOR, org_unit_id=U(), valid_from=date(2025, 1, 1))
    with pytest.raises(ValidationError):
        UserRoleAssignmentCreate(role=UserRole.STUDENT, org_unit_id=U(), valid_from=date(2025, 1, 1),
                                 unknown_field=1)


def test_llm_requires_pii_masking() -> None:
    AiRunCreate(task_type=AiTaskType.OCR, model_id=U(), document_version_id=U(), pii_masked=False)
    with pytest.raises(ValidationError, match="анонімізований"):
        AiRunCreate(task_type=AiTaskType.COMPLIANCE_CHECK, model_id=U(), document_version_id=U(), pii_masked=False)


def test_confidence_bounds_and_review_flag() -> None:
    with pytest.raises(ValidationError):
        AiRunResult(status=AiRunStatus.SUCCEEDED, confidence=Decimal("1.2"))
    ok = AiRunResult(status=AiRunStatus.SUCCEEDED, confidence=Decimal("0.95"))
    assert ok.requires_human_review is False
    low_field = AiRunResult(status=AiRunStatus.SUCCEEDED, confidence=Decimal("0.95"),
                            extracted_fields=[dict(field_key="amount_uah", extracted_value=1000,
                                                   confidence=Decimal("0.6"))])
    assert low_field.requires_human_review is True
    with pytest.raises(ValidationError, match="error_message"):
        AiRunResult(status=AiRunStatus.FAILED)


def test_findings_need_verified_citation() -> None:
    FindingIn(**finding())
    with pytest.raises(ValidationError, match="перевіреної цитати"):
        FindingIn(**finding(citations=[dict(chunk_id=U(), quoted_text="x", quote_verified=False)]))
    with pytest.raises(ValidationError):
        FindingIn(**finding(citations=[]))
    with pytest.raises(ValidationError, match="compliant"):
        ComplianceCheckIn(verdict=ComplianceVerdict.COMPLIANT, regulations_as_of=date.today(),
                          findings=[finding(severity=FindingSeverity.CRITICAL)])


def test_step_decision_and_routes() -> None:
    StepDecision(decision=StepStatus.APPROVED)
    with pytest.raises(ValidationError, match="обов'язкові"):
        StepDecision(decision=StepStatus.REJECTED, comment="  ")
    with pytest.raises(ValidationError):
        StepDecision(decision=StepStatus.SKIPPED)
    with pytest.raises(ValidationError, match="ШІ"):
        ApprovalRouteCreate(document_version_id=U(), origin="ai_suggested",
                            steps=[dict(stage_no=1, ordinal=1, step_kind=StepKind.VISA, assignee_id=U())])
    with pytest.raises(ValidationError, match="передувати підписанню"):
        RouteTemplateCreate(code="ORDER_MAIN", name_uk="Наказ", document_type_id=U(), steps=[
            dict(stage_no=1, ordinal=1, step_kind=StepKind.SIGNING, assignee_rule="role", role=UserRole.RECTOR),
            dict(stage_no=2, ordinal=1, step_kind=StepKind.VISA, assignee_rule="author_unit_role",
                 role=UserRole.DEPARTMENT_HEAD),
        ])
    tpl = RouteTemplateCreate(code="ORDER_MAIN", name_uk="Наказ", document_type_id=U(), steps=[
        dict(stage_no=1, ordinal=1, step_kind=StepKind.VISA, assignee_rule="author_unit_role",
             role=UserRole.INSTITUTE_DIRECTOR),
        dict(stage_no=2, ordinal=1, step_kind=StepKind.SIGNING, assignee_rule="role", role=UserRole.RECTOR),
    ])
    assert tpl.steps[1].requires_kep is True


def test_status_transitions() -> None:
    check_transition(DocumentStatus.DRAFT, DocumentStatus.ON_APPROVAL)
    with pytest.raises(ValueError):
        check_transition(DocumentStatus.SIGNED, DocumentStatus.DRAFT)
