"""DTO відповідей і запитів API маршрутизації (Pydantic v2)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from sed_db.enums import (
    RouteStatus,
    SignatureProvider,
    SignatureValidation,
    StepKind,
    StepStatus,
    UserRole,
)
from sed_db.schemas.common import InputModel


class AssigneeOverride(InputModel):
    stage_no: int = Field(ge=1)
    ordinal: int = Field(ge=1)
    user_id: uuid.UUID


class RouteRequest(InputModel):
    template_code: str | None = Field(default=None, max_length=64,
                                      description="Немає — шаблон добирається за видом і підрозділом документа")
    assignee_overrides: list[AssigneeOverride] = Field(default_factory=list, max_length=50)


class RouteCreate(RouteRequest):
    start: bool = Field(default=True, description="Одразу запустити маршрут (перший етап отримає документ)")
    acknowledge_findings: bool = Field(
        default=False, description="Ініціатор свідомо запускає маршрут попри відкриті критичні зауваження комплаєнсу")


class RouteStart(InputModel):
    acknowledge_findings: bool = False


class RouteCancel(InputModel):
    reason: str = Field(min_length=3, max_length=1024)


class SignRequest(InputModel):
    """Віза з КЕП: відокремлений CAdES над SHA-256 версії документа (base64 DER)."""

    signature_base64: str = Field(min_length=16, max_length=400_000)
    provider: SignatureProvider = SignatureProvider.FILE_KEY
    decision: Literal[StepStatus.APPROVED, StepStatus.APPROVED_WITH_REMARKS] = StepStatus.APPROVED
    comment: str | None = Field(default=None, max_length=10_000)

    @model_validator(mode="after")
    def _remarks(self) -> SignRequest:
        if self.decision is StepStatus.APPROVED_WITH_REMARKS and not (self.comment and self.comment.strip()):
            raise ValueError("Для візи із зауваженнями потрібен текст зауважень")
        return self


class PlannedStepView(BaseModel):
    stage_no: int
    ordinal: int
    title_uk: str
    step_kind: StepKind
    assignee_id: uuid.UUID
    assignee_name: str
    acting_role: UserRole | None
    requires_kep: bool
    is_mandatory: bool
    deadline_hours: int | None
    will_skip: bool
    skip_reason: str | None


class RoutePlanView(BaseModel):
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    template_code: str
    template_version: int
    template_name: str
    steps: list[PlannedStepView]


class SignatureView(BaseModel):
    id: uuid.UUID
    cert_subject_cn: str
    cert_serial: str
    cert_issuer: str
    signed_at: datetime
    validation_status: SignatureValidation


class StepView(BaseModel):
    id: uuid.UUID
    stage_no: int
    ordinal: int
    title_uk: str
    step_kind: StepKind
    assignee_id: uuid.UUID
    assignee_name: str
    original_assignee_id: uuid.UUID | None
    original_assignee_name: str | None
    acting_role: UserRole | None
    status: StepStatus
    is_mandatory: bool
    requires_kep: bool
    activated_at: datetime | None
    due_at: datetime | None
    decided_at: datetime | None
    decision_comment: str | None
    is_overdue: bool
    signature: SignatureView | None


class RouteView(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    document_version_id: uuid.UUID
    content_sha256: str
    template_code: str | None
    template_version: int | None
    template_name: str | None
    status: RouteStatus
    current_stage: int | None
    initiated_by_id: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    cancel_reason: str | None
    steps: list[StepView]
    warnings: list[str] = Field(default_factory=list)


class InboxItem(BaseModel):
    step_id: uuid.UUID
    route_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    document_type_code: str
    step_kind: StepKind
    title_uk: str
    requires_kep: bool
    activated_at: datetime | None
    due_at: datetime | None
    is_overdue: bool
    is_delegated: bool


class SigningRequestView(BaseModel):
    """Що саме підписувати: клієнт (ІІТ / Дія.Підпис) рахує той самий геш над файлом версії."""

    step_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    file_name: str
    content_sha256: str
    digest_algorithm: Literal["SHA-256"] = "SHA-256"
    signature_container: Literal["CAdES detached (CMS SignedData)"] = "CAdES detached (CMS SignedData)"
    accepted_formats: list[str] = Field(default_factory=lambda: ["CAdES-BES", "CAdES-X-Long"])
    not_before: datetime | None
    accepted_levels: list[str] = Field(default_factory=lambda: ["qualified"],
                                       description="qualified — КЕП КНЕДП; internal — внутрішній ключ АІ-СЕД ПУЕТ")
    accepted_levels_uk: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------- ключі КЕП

KeyLevel = Literal["qualified", "internal"]


class ChallengeView(BaseModel):
    """Що підписати своїм ключем, щоб довести володіння ним (proof of possession)."""

    challenge_token: str
    data_base64: str = Field(description="Байти виклику: підписати як файл (CAdES detached)")
    data_text: str
    content_sha256: str
    expires_at: datetime
    instructions_uk: str


class KeyRegisterRequest(InputModel):
    challenge_token: str = Field(min_length=20, max_length=2048)
    signature_base64: str = Field(min_length=16, max_length=400_000,
                                  description="CAdES detached над data виклику (віджет ІІТ / Дія.Підпис / keytool)")
    key_format: Literal["jks", "pkcs12", "key6_dat", "token", "cloud"] | None = Field(
        default=None, description="Звідки ключ (довідково): ПриватБанк — jks, Дія/ІІТ — key6_dat або pkcs12")


class KeyBindingView(BaseModel):
    id: uuid.UUID
    level: KeyLevel
    level_uk: str
    subject_cn: str
    issuer: str
    serial: str
    fingerprint_sha256: str
    valid_from: datetime
    valid_to: datetime
    key_format: str | None
    bound_via: str
    bound_at: datetime | None
    revoked_at: datetime | None
    revoke_reason: str | None
    expires_soon: bool


class KeyRevokeRequest(InputModel):
    reason: str = Field(min_length=3, max_length=256)


class CertificateCheckView(BaseModel):
    """Розбір сертифіката. Особистий ключ сюди не надсилається: файл-ключ перевіряє `keytool inspect`."""

    level: Literal["qualified", "internal", "mock", "other"]
    level_uk: str
    certificate: dict
    usable_for_signing: bool
    problems: list[str]
    trusted_issuer: bool | None = Field(description="None — сертифікат ДСТУ: ланцюжок перевіряє провайдер КНЕДП")
    registered_to_me: bool


class SignatureVerifyRequest(InputModel):
    signature_base64: str = Field(min_length=16, max_length=400_000)
    content_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class SignatureVerifyView(BaseModel):
    status: SignatureValidation
    level: Literal["qualified", "internal", "mock", "other"] | None
    reasons: list[str]
    report: dict


class CsrEnrollRequest(InputModel):
    csr_pem: str = Field(min_length=100, max_length=20_000,
                         description="PKCS#10, згенерований `keytool new-key` на комп'ютері користувача")


class KeyFileRequest(InputModel):
    password: str = Field(min_length=12, max_length=128,
                          description="Пароль до файлу-ключа; сервер його не зберігає")


class InternalCertView(BaseModel):
    serial: str
    subject_cn: str
    fingerprint_sha256: str
    valid_from: datetime
    valid_to: datetime
    certificate_pem: str
    chain_pem: str
    notice_uk: str
