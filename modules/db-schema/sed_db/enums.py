"""Довідники-перелічення домену СЕД.

Значення (`value`) зберігаються в БД як нативні ENUM PostgreSQL; українські
підписи для UI живуть у `LABELS_UK`, щоб не змішувати локалізацію зі схемою.
Нові значення додаються міграцією `ALTER TYPE ... ADD VALUE`.
"""

from __future__ import annotations

from enum import StrEnum


# --- Організаційна структура -------------------------------------------------

class OrgUnitKind(StrEnum):
    UNIVERSITY = "university"
    RECTORATE = "rectorate"
    INSTITUTE = "institute"            # навчально-науковий інститут
    FACULTY = "faculty"
    DEPARTMENT = "department"          # кафедра
    DIVISION = "division"              # відділ, управління, сектор
    CENTER = "center"                  # навчально-науковий центр
    COLLEGIAL_BODY = "collegial_body"  # вчена рада, НМР, ректорат як орган


# --- Ролі користувачів --------------------------------------------------------

class UserRole(StrEnum):
    """Функціональні ролі СЕД (джерело — Keycloak realm roles, тут дзеркало з областю дії)."""

    STUDENT = "student"                                  # здобувач вищої освіти
    LECTURER = "lecturer"                                # викладач
    DEPARTMENT_HEAD = "department_head"                  # завідувач кафедри
    INSTITUTE_DIRECTOR = "institute_director"            # директор інституту
    UNIT_HEAD = "unit_head"                              # керівник структурного підрозділу
    RECTOR = "rector"
    VICE_RECTOR = "vice_rector"                          # проректор
    STAFF = "staff"                                      # співробітник
    STUDENT_GOVERNMENT_HEAD = "student_government_head"  # голова студентського самоврядування


# Ролі, що діють у межах конкретного підрозділу (org_unit_id обов'язковий).
UNIT_SCOPED_ROLES: frozenset[UserRole] = frozenset(
    {
        UserRole.STUDENT,
        UserRole.LECTURER,
        UserRole.DEPARTMENT_HEAD,
        UserRole.INSTITUTE_DIRECTOR,
        UserRole.UNIT_HEAD,
        UserRole.STAFF,
    }
)

# Ролі керівників: одночасно не більше однієї особи на підрозділ (на університет — для ректора).
EXCLUSIVE_ROLES: frozenset[UserRole] = frozenset(
    {
        UserRole.DEPARTMENT_HEAD,
        UserRole.INSTITUTE_DIRECTOR,
        UserRole.UNIT_HEAD,
        UserRole.RECTOR,
        UserRole.STUDENT_GOVERNMENT_HEAD,
    }
)

# Якого виду має бути підрозділ для ролі (перевіряє тригер sed_check_role_scope).
ROLE_UNIT_KIND: dict[UserRole, OrgUnitKind] = {
    UserRole.DEPARTMENT_HEAD: OrgUnitKind.DEPARTMENT,
    UserRole.INSTITUTE_DIRECTOR: OrgUnitKind.INSTITUTE,
}


# --- Документи ----------------------------------------------------------------

class DocumentDirection(StrEnum):
    INTERNAL = "internal"
    INCOMING = "incoming"
    OUTGOING = "outgoing"


class DocumentStatus(StrEnum):
    DRAFT = "draft"
    AI_REVIEW = "ai_review"                  # автоматичні перевірки ШІ (не рішення)
    ON_APPROVAL = "on_approval"              # маршрут погодження активний
    REVISION_REQUIRED = "revision_required"  # повернуто на доопрацювання
    APPROVED = "approved"                    # всі візи отримано, очікує підпису
    SIGNED = "signed"                        # накладено КЕП підписанта
    REGISTERED = "registered"                # присвоєно реєстраційний індекс
    IN_EXECUTION = "in_execution"
    EXECUTED = "executed"
    ARCHIVED = "archived"
    CANCELLED = "cancelled"


class AccessLevel(StrEnum):
    PUBLIC = "public"                  # публічна інформація
    INTERNAL = "internal"              # для внутрішнього користування
    RESTRICTED = "restricted"          # обмежений доступ (службова інформація)
    PERSONAL_DATA = "personal_data"    # містить персональні дані (ЗУ «Про захист ПД»)


class VersionSource(StrEnum):
    UPLOAD = "upload"
    EDITOR = "editor"
    TEMPLATE = "template"
    AI_GENERATED = "ai_generated"      # проєкт від генератора, обов'язково перевіряє людина
    OCR_SCAN = "ocr_scan"


class DocumentLinkType(StrEnum):
    EXECUTES = "executes"        # на виконання
    AMENDS = "amends"            # вносить зміни
    REVOKES = "revokes"          # скасовує / визнає таким, що втратив чинність
    REPLIES_TO = "replies_to"    # відповідь на
    SUPPLEMENTS = "supplements"  # доповнює
    BASED_ON = "based_on"        # на підставі (заяви, службової записки)


# --- Маршрути погодження -----------------------------------------------------

class RouteStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class RouteOrigin(StrEnum):
    MANUAL = "manual"
    TEMPLATE = "template"
    AI_SUGGESTED = "ai_suggested"


class StepKind(StrEnum):
    VISA = "visa"                        # візування
    APPROVAL = "approval"                # погодження
    SIGNING = "signing"                  # підписання (КЕП)
    RATIFICATION = "ratification"        # затвердження
    ACKNOWLEDGEMENT = "acknowledgement"  # ознайомлення
    REGISTRATION = "registration"        # реєстрація в журналі


class StepStatus(StrEnum):
    WAITING = "waiting"                  # етап ще не настав
    ACTIVE = "active"
    APPROVED = "approved"
    APPROVED_WITH_REMARKS = "approved_with_remarks"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    DELEGATED = "delegated"
    EXPIRED = "expired"


class AssigneeRule(StrEnum):
    SPECIFIC_USER = "specific_user"
    POSITION_IN_UNIT = "position_in_unit"   # посада в підрозділі
    UNIT_HEAD = "unit_head"                 # керівник вказаного підрозділу
    AUTHOR_UNIT_HEAD = "author_unit_head"   # керівник підрозділу автора
    ROLE = "role"                           # власник ролі (за потреби — у вказаному підрозділі)
    AUTHOR_UNIT_ROLE = "author_unit_role"   # власник ролі в підрозділі автора (напр. директор його інституту)


# --- КЕП ----------------------------------------------------------------------

class SignatureFormat(StrEnum):
    CADES_BES = "CAdES-BES"
    CADES_X_LONG = "CAdES-X-Long"
    XADES_B_LT = "XAdES-B-LT"
    PADES_B_LT = "PAdES-B-LT"
    ASIC_E = "ASiC-E"


class SignatureProvider(StrEnum):
    FILE_KEY = "file_key"           # файловий ключ (Key-6.dat, .pfx)
    HARDWARE_TOKEN = "hardware_token"
    CLOUD_KEP = "cloud_kep"         # хмарний КЕП НКЕП
    DIIA_SIGNATURE = "diia_signature"


class SignatureValidation(StrEnum):
    PENDING = "pending"
    VALID = "valid"
    INVALID = "invalid"
    CERT_REVOKED = "cert_revoked"
    CERT_EXPIRED = "cert_expired"
    INDETERMINATE = "indeterminate"


# --- ШІ ----------------------------------------------------------------------

class AiTaskType(StrEnum):
    OCR = "ocr"
    CLASSIFICATION = "classification"
    NER_EXTRACTION = "ner_extraction"
    COMPLIANCE_CHECK = "compliance_check"
    ROUTE_SUGGESTION = "route_suggestion"
    ANOMALY_DETECTION = "anomaly_detection"
    DOCUMENT_GENERATION = "document_generation"
    PII_MASKING = "pii_masking"


# Задачі, що передають текст у контекстне вікно LLM: PII має бути замасковано.
LLM_TASKS: frozenset[AiTaskType] = frozenset(
    {
        AiTaskType.CLASSIFICATION,
        AiTaskType.NER_EXTRACTION,
        AiTaskType.COMPLIANCE_CHECK,
        AiTaskType.ROUTE_SUGGESTION,
        AiTaskType.DOCUMENT_GENERATION,
    }
)


class AiRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ModelProvider(StrEnum):
    VLLM = "vllm"
    OLLAMA = "ollama"
    LOCAL_OTHER = "local_other"   # DocTR, PaddleOCR, spaCy тощо


class HumanReviewStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CORRECTED = "corrected"
    REJECTED = "rejected"


class ComplianceVerdict(StrEnum):
    COMPLIANT = "compliant"
    ISSUES_FOUND = "issues_found"
    NON_COMPLIANT = "non_compliant"
    INCONCLUSIVE = "inconclusive"


class FindingSeverity(StrEnum):
    INFO = "info"
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class FindingCategory(StrEnum):
    CONTRADICTION = "contradiction"              # суперечність чинному акту
    OUTDATED_REFERENCE = "outdated_reference"    # посилання на нечинний акт
    MISSING_REQUISITE = "missing_requisite"      # відсутній реквізит (ДСТУ 4163)
    DSTU_FORMATTING = "dstu_formatting"
    COMPETENCE_VIOLATION = "competence_violation"  # перевищення повноважень
    DEADLINE_VIOLATION = "deadline_violation"
    PERSONAL_DATA = "personal_data"
    OTHER = "other"


class FindingStatus(StrEnum):
    OPEN = "open"
    ACCEPTED = "accepted"      # людина погодилась, виправлення потрібне
    DISMISSED = "dismissed"    # людина відхилила як хибне спрацювання
    FIXED = "fixed"


class RegulatoryLevel(StrEnum):
    LAW = "law"                 # закони України (ВРУ)
    CABINET = "cabinet"         # постанови КМУ
    MINISTRY = "ministry"       # накази МОН
    STANDARD = "standard"       # ДСТУ
    UNIVERSITY = "university"   # Статут, положення ПУЕТ
    UNIT = "unit"               # положення підрозділів


class RegulatoryStatus(StrEnum):
    DRAFT = "draft"
    IN_FORCE = "in_force"
    AMENDED = "amended"
    REPEALED = "repealed"


# --- Аудит --------------------------------------------------------------------

class AuditEventCategory(StrEnum):
    DATA_CHANGE = "data_change"        # рядкова зміна (тригер)
    AI_ANALYSIS = "ai_analysis"        # результат аналізу ШІ: confidence, порушення, метадані
    BUSINESS_EVENT = "business_event"  # перегляд, експорт, завантаження файлу


class AuditActorType(StrEnum):
    USER = "user"
    SYSTEM = "system"
    AI_SERVICE = "ai_service"


LABELS_UK: dict[StrEnum, str] = {
    DocumentStatus.DRAFT: "Проєкт",
    DocumentStatus.AI_REVIEW: "Автоматична перевірка",
    DocumentStatus.ON_APPROVAL: "На погодженні",
    DocumentStatus.REVISION_REQUIRED: "На доопрацюванні",
    DocumentStatus.APPROVED: "Погоджено",
    DocumentStatus.SIGNED: "Підписано",
    DocumentStatus.REGISTERED: "Зареєстровано",
    DocumentStatus.IN_EXECUTION: "На виконанні",
    DocumentStatus.EXECUTED: "Виконано",
    DocumentStatus.ARCHIVED: "В архіві",
    DocumentStatus.CANCELLED: "Скасовано",
    StepKind.VISA: "Візування",
    StepKind.APPROVAL: "Погодження",
    StepKind.SIGNING: "Підписання",
    StepKind.RATIFICATION: "Затвердження",
    StepKind.ACKNOWLEDGEMENT: "Ознайомлення",
    StepKind.REGISTRATION: "Реєстрація",
    UserRole.STUDENT: "Студент",
    UserRole.LECTURER: "Викладач",
    UserRole.DEPARTMENT_HEAD: "Завідувач кафедри",
    UserRole.INSTITUTE_DIRECTOR: "Директор інституту",
    UserRole.UNIT_HEAD: "Керівник структурного підрозділу",
    UserRole.RECTOR: "Ректор",
    UserRole.VICE_RECTOR: "Проректор",
    UserRole.STAFF: "Співробітник",
    UserRole.STUDENT_GOVERNMENT_HEAD: "Голова студентського самоврядування",
    HumanReviewStatus.PENDING: "Очікує ручної валідації",
    HumanReviewStatus.CONFIRMED: "Підтверджено оператором",
    HumanReviewStatus.CORRECTED: "Виправлено оператором",
    HumanReviewStatus.REJECTED: "Відхилено оператором",
}
