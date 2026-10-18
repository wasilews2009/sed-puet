"""Довідкові дані: службовий користувач, види документів із позначками ПД, промпт інжесту.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-17 18:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from sed_db.enums import DocumentDirection
from sed_db.models import DocumentType, PromptTemplate, User
from sed_db.seeds import (
    DOCUMENT_TYPES,
    PROMPTS,
    SYSTEM_USER_ID,
    SYSTEM_USER_KEYCLOAK_SUB,
    load_prompt,
    requisites_schema,
)

revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(insert(User.__table__).values(
        id=SYSTEM_USER_ID, keycloak_sub=SYSTEM_USER_KEYCLOAK_SUB, email="sed-system@puet.edu.ua",
        last_name="СЕД", first_name="Система", is_active=False,
    ).on_conflict_do_nothing(index_elements=["id"]))

    # DO NOTHING: правки адміністратора в довіднику не перезаписуються повторним накатом.
    for code, name_uk, access_level, required in DOCUMENT_TYPES:
        bind.execute(insert(DocumentType.__table__).values(
            code=code, name_uk=name_uk, default_direction=DocumentDirection.INTERNAL.value,
            default_access_level=access_level.value, requires_kep=True,
            requisites_schema=requisites_schema(required),
        ).on_conflict_do_nothing(index_elements=["code"]))

    for code, version, task_type, file_name, sha in PROMPTS:
        bind.execute(insert(PromptTemplate.__table__).values(
            code=code, version=version, task_type=task_type.value,
            body=load_prompt(file_name, sha), body_sha256=sha, created_by_id=SYSTEM_USER_ID,
        ).on_conflict_do_nothing(index_elements=["code", "version"]))


def downgrade() -> None:
    # Падає на FK, якщо довідники вже використано, і це навмисно.
    op.execute("ALTER TABLE prompt_templates DISABLE TRIGGER trg_prompt_templates_append_only")
    for code, version, *_ in PROMPTS:
        op.execute(sa.text("DELETE FROM prompt_templates WHERE code = :c AND version = :v")
                   .bindparams(c=code, v=version))
    op.execute("ALTER TABLE prompt_templates ENABLE TRIGGER trg_prompt_templates_append_only")
    op.execute(sa.text("DELETE FROM document_types WHERE code = ANY(:codes)")
               .bindparams(codes=[c for c, *_ in DOCUMENT_TYPES]))
    op.execute(sa.text("DELETE FROM users WHERE id = :id").bindparams(id=SYSTEM_USER_ID))
