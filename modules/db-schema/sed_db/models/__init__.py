"""Імпорт усіх моделей, щоб `Base.metadata` бачила повну схему (для Alembic)."""

from .ai import (
    AiExtractedField,
    AiModel,
    AiRun,
    ComplianceCheck,
    ComplianceCitation,
    ComplianceFinding,
    PromptTemplate,
    RegulatoryChunk,
    RegulatorySource,
)
from .audit import AuditLog
from .documents import (
    Document,
    DocumentLink,
    DocumentType,
    DocumentVersion,
    NomenclatureCase,
    RegistrationJournal,
    VersionAttachment,
)
from .org import Delegation, OrgUnit, StaffAssignment, User, UserRoleAssignment
from .routing import (
    ApprovalRoute,
    ApprovalStep,
    ApprovalStepHistory,
    DocumentStatusHistory,
    RouteTemplate,
    RouteTemplateStep,
)
from .signatures import Signature

__all__ = [
    "AiExtractedField",
    "AiModel",
    "AiRun",
    "ApprovalRoute",
    "ApprovalStep",
    "ApprovalStepHistory",
    "AuditLog",
    "ComplianceCheck",
    "ComplianceCitation",
    "ComplianceFinding",
    "Delegation",
    "Document",
    "DocumentLink",
    "DocumentStatusHistory",
    "DocumentType",
    "DocumentVersion",
    "NomenclatureCase",
    "OrgUnit",
    "PromptTemplate",
    "RegistrationJournal",
    "RegulatoryChunk",
    "RegulatorySource",
    "RouteTemplate",
    "RouteTemplateStep",
    "Signature",
    "StaffAssignment",
    "User",
    "UserRoleAssignment",
    "VersionAttachment",
]
