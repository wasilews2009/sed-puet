"""Документи, версії, додатки, номенклатура справ і журнали реєстрації."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    BigInteger,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, TimestampMixin, UUIDPkMixin, pg_enum
from ..enums import (
    AccessLevel,
    DocumentDirection,
    DocumentLinkType,
    DocumentStatus,
    VersionSource,
)

SHA256_HEX_CHECK = "~ '^[0-9a-f]{64}$'"


class DocumentType(UUIDPkMixin, TimestampMixin, Base):
    """Вид документа: наказ з основної діяльності, наказ о/с, розпорядження, заява…"""

    __tablename__ = "document_types"

    code: Mapped[str] = mapped_column(String(64), unique=True)  # ORDER_MAIN, MEMO …
    name_uk: Mapped[str] = mapped_column(String(256))
    default_direction: Mapped[DocumentDirection] = mapped_column(
        pg_enum(DocumentDirection, "document_direction"),
        server_default=DocumentDirection.INTERNAL.value,
    )
    default_access_level: Mapped[AccessLevel] = mapped_column(
        pg_enum(AccessLevel, "access_level"), server_default=AccessLevel.INTERNAL.value
    )
    requires_kep: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Ключ шаблону генератора (реквізити й структура тексту за ДСТУ 4163:2020).
    dstu_template_key: Mapped[str | None] = mapped_column(String(128))
    # JSON Schema реквізитів, які NER має витягти для цього виду документа.
    requisites_schema: Mapped[dict[str, Any] | None]
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class NomenclatureCase(UUIDPkMixin, TimestampMixin, Base):
    """Справа зі зведеної номенклатури справ ПУЕТ на рік."""

    __tablename__ = "nomenclature_cases"

    year: Mapped[int] = mapped_column(SmallInteger)
    case_index: Mapped[str] = mapped_column(String(32))  # напр. «02-07»
    title_uk: Mapped[str] = mapped_column(String(1024))
    org_unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("org_units.id", ondelete="RESTRICT"), index=True
    )
    retention_term: Mapped[str] = mapped_column(String(64))  # «Пост.», «5 р.», «75 р.»
    retention_article: Mapped[str | None] = mapped_column(String(64))  # стаття Переліку
    closed_at: Mapped[datetime | None]

    __table_args__ = (
        UniqueConstraint("year", "case_index"),
        CheckConstraint("year BETWEEN 2000 AND 2100", name="year_range"),
    )


class RegistrationJournal(UUIDPkMixin, TimestampMixin, Base):
    """Журнал реєстрації з атомарним лічильником порядкових номерів на рік."""

    __tablename__ = "registration_journals"

    code: Mapped[str] = mapped_column(String(64))
    name_uk: Mapped[str] = mapped_column(String(256))
    year: Mapped[int] = mapped_column(SmallInteger)
    document_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_types.id", ondelete="RESTRICT")
    )
    # Шаблон індексу: {seq} — порядковий номер, {suffix} — літерний індекс (напр. «-к»).
    number_pattern: Mapped[str] = mapped_column(String(64), server_default="{seq}")
    suffix: Mapped[str | None] = mapped_column(String(16))
    last_seq: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    __table_args__ = (
        UniqueConstraint("code", "year"),
        CheckConstraint("last_seq >= 0", name="last_seq_non_negative"),
    )


class Document(UUIDPkMixin, TimestampMixin, Base):
    """Реєстраційно-контрольна картка документа (метадані без вмісту файлу)."""

    __tablename__ = "documents"

    document_type_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_types.id", ondelete="RESTRICT"), index=True
    )
    direction: Mapped[DocumentDirection] = mapped_column(
        pg_enum(DocumentDirection, "document_direction")
    )
    status: Mapped[DocumentStatus] = mapped_column(
        pg_enum(DocumentStatus, "document_status"), server_default=DocumentStatus.DRAFT.value
    )
    access_level: Mapped[AccessLevel] = mapped_column(pg_enum(AccessLevel, "access_level"))

    title: Mapped[str] = mapped_column(String(1024))  # заголовок до тексту
    summary: Mapped[str | None] = mapped_column(Text)
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    org_unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("org_units.id", ondelete="RESTRICT"), index=True
    )
    nomenclature_case_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("nomenclature_cases.id", ondelete="RESTRICT")
    )

    # Реєстрація (заповнюється після підписання).
    journal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("registration_journals.id", ondelete="RESTRICT")
    )
    registration_seq: Mapped[int | None] = mapped_column(Integer)
    registration_number: Mapped[str | None] = mapped_column(String(64))
    registration_date: Mapped[date | None]

    # Вхідні: реквізити кореспондента.
    correspondent_name: Mapped[str | None] = mapped_column(String(512))
    correspondent_number: Mapped[str | None] = mapped_column(String(64))
    correspondent_date: Mapped[date | None]

    due_date: Mapped[date | None]           # контрольний строк виконання
    effective_from: Mapped[date | None]     # набрання чинності
    executed_at: Mapped[datetime | None]

    # Реквізити після валідації людиною (а не сирий вихід NER, той — в ai_extracted_fields).
    requisites: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))

    current_version_id: Mapped[uuid.UUID | None] = mapped_column()

    # Оптимістичне блокування: конкурентні редагування картки не перетирають одне одного.
    row_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    deleted_at: Mapped[datetime | None]

    search_tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('simple', coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('simple', coalesce(registration_number, '')), 'A') || "
            "setweight(to_tsvector('simple', coalesce(summary, '')), 'B')",
            persisted=True,
        ),
    )

    versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="document",
        foreign_keys="DocumentVersion.document_id",
        order_by="DocumentVersion.version_number",
        lazy="raise",
    )
    current_version: Mapped[DocumentVersion | None] = relationship(
        foreign_keys=[current_version_id],
        primaryjoin="Document.current_version_id == DocumentVersion.id",
        post_update=True,
        lazy="raise",
    )

    __mapper_args__ = {"version_id_col": row_version}

    __table_args__ = (
        # Поточна версія обов'язково належить цьому ж документу (складений FK).
        ForeignKeyConstraint(
            ["current_version_id", "id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_documents_current_version_same_document",
            use_alter=True,
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status NOT IN ('registered','in_execution','executed','archived') OR "
            "(registration_number IS NOT NULL AND registration_date IS NOT NULL)",
            name="registered_has_number",
        ),
        CheckConstraint(
            "(journal_id IS NULL) = (registration_seq IS NULL)", name="journal_seq_together"
        ),
        CheckConstraint(
            "direction <> 'incoming' OR correspondent_name IS NOT NULL",
            name="incoming_has_correspondent",
        ),
        CheckConstraint("row_version > 0", name="row_version_positive"),
        UniqueConstraint("journal_id", "registration_seq"),
        Index("ix_documents_search_tsv", "search_tsv", postgresql_using="gin"),
        Index(
            "ix_documents_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index("ix_documents_status_due", "status", "due_date", postgresql_where=text("deleted_at IS NULL")),
        Index("ix_documents_registration_number", "registration_number"),
    )


class DocumentVersion(UUIDPkMixin, Base):
    """Незмінна версія вмісту. Файл — у MinIO; тут — адреса, хеш і метадані.

    Редагування = нова версія. Після передачі на погодження/підпису версія
    блокується (`is_locked`), тригер забороняє зміну її вмісту.
    """

    __tablename__ = "document_versions"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), index=True
    )
    version_number: Mapped[int] = mapped_column(Integer)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT")
    )
    source: Mapped[VersionSource] = mapped_column(pg_enum(VersionSource, "version_source"))
    # Якщо версію згенеровано/розпізнано ШІ — посилання на запуск для відтворюваності.
    ai_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_runs.id", ondelete="RESTRICT", use_alter=True)
    )

    storage_bucket: Mapped[str] = mapped_column(String(63))
    storage_key: Mapped[str] = mapped_column(String(1024))
    storage_version_id: Mapped[str | None] = mapped_column(String(128))  # S3 object versioning
    file_name: Mapped[str] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_sha256: Mapped[str] = mapped_column(String(64))
    page_count: Mapped[int | None] = mapped_column(Integer)

    # Текстовий шар (з PDF або OCR) для повнотекстового пошуку й RAG.
    extracted_text: Mapped[str | None] = mapped_column(Text)
    text_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', coalesce(extracted_text, ''))", persisted=True),
    )

    change_note: Mapped[str | None] = mapped_column(String(1024))
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    is_locked: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    locked_at: Mapped[datetime | None]

    document: Mapped[Document] = relationship(
        back_populates="versions", foreign_keys=[document_id], lazy="raise"
    )
    attachments: Mapped[list[VersionAttachment]] = relationship(
        back_populates="version", order_by="VersionAttachment.ordinal", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint("document_id", "version_number"),
        # Ціль для складених FK «версія саме цього документа».
        UniqueConstraint("id", "document_id", name="uq_document_versions_id_document"),
        UniqueConstraint("storage_bucket", "storage_key", "storage_version_id",
                         postgresql_nulls_not_distinct=True),
        CheckConstraint("version_number > 0", name="version_number_positive"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint(f"content_sha256 {SHA256_HEX_CHECK}", name="sha256_format"),
        CheckConstraint("parent_version_id IS NULL OR parent_version_id <> id", name="not_self_parent"),
        CheckConstraint("is_locked = (locked_at IS NOT NULL)", name="lock_timestamp"),
        Index("ix_document_versions_text_tsv", "text_tsv", postgresql_using="gin"),
        Index("ix_document_versions_sha256", "content_sha256"),
    )


class VersionAttachment(UUIDPkMixin, Base):
    """Додаток до конкретної версії (входить до підписуваного пакета)."""

    __tablename__ = "version_attachments"

    document_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT"), index=True
    )
    ordinal: Mapped[int] = mapped_column(SmallInteger)  # «Додаток 1»
    title: Mapped[str] = mapped_column(String(512))
    storage_bucket: Mapped[str] = mapped_column(String(63))
    storage_key: Mapped[str] = mapped_column(String(1024))
    mime_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))

    version: Mapped[DocumentVersion] = relationship(back_populates="attachments", lazy="raise")

    __table_args__ = (
        UniqueConstraint("document_version_id", "ordinal"),
        CheckConstraint("ordinal > 0", name="ordinal_positive"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint(f"content_sha256 {SHA256_HEX_CHECK}", name="sha256_format"),
    )


class DocumentLink(UUIDPkMixin, Base):
    """Зв'язки між документами: «на виконання», «скасовує», «вносить зміни»…"""

    __tablename__ = "document_links"

    source_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), index=True
    )
    target_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), index=True
    )
    link_type: Mapped[DocumentLinkType] = mapped_column(
        pg_enum(DocumentLinkType, "document_link_type")
    )
    note: Mapped[str | None] = mapped_column(String(512))  # напр. «п. 3 визнати таким, що втратив чинність»
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("source_document_id", "target_document_id", "link_type"),
        CheckConstraint("source_document_id <> target_document_id", name="not_self"),
    )
