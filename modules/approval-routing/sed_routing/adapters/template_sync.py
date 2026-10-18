"""Синхронізація каталогу шаблонів (TOML) у `route_templates` / `route_template_steps`.

Шаблони версіонуються: якщо структура змінилась, додається рядок `version + 1`, а попередня
версія деактивується. Запущені маршрути продовжують посилатися на свою версію.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sed_db.models import DocumentType, OrgUnit, RouteTemplate, RouteTemplateStep, User
from sed_db.schemas import RouteTemplateCreate, RouteTemplateStepIn

from ..domain.errors import TemplateConfigError
from ..domain.templates import TemplateCatalog, TemplateSpec


@dataclass(frozen=True)
class SyncResult:
    code: str
    version: int
    action: str  # created | updated | unchanged | error
    error: str | None = None


def _structure(steps) -> list[tuple]:  # noqa: ANN001
    return sorted(
        (s.stage_no, s.ordinal, s.step_kind, s.assignee_rule, s.assignee_user_id, s.assignee_org_unit_id,
         s.position_code, s.role, s.deadline_hours, s.is_mandatory, s.requires_kep)
        for s in steps)


async def _unit_id(s: AsyncSession, code: str | None, template: str) -> uuid.UUID | None:
    if code is None:
        return None
    uid = await s.scalar(select(OrgUnit.id).where(OrgUnit.code == code, OrgUnit.is_active))
    if uid is None:
        raise TemplateConfigError(f"Шаблон {template}: підрозділ з кодом {code} не знайдено в оргструктурі")
    return uid


async def build_dto(s: AsyncSession, spec: TemplateSpec) -> RouteTemplateCreate:
    type_id = await s.scalar(select(DocumentType.id).where(DocumentType.code == spec.document_type))
    if type_id is None:
        raise TemplateConfigError(f"Шаблон {spec.code}: вид документа {spec.document_type} не знайдено")
    steps = []
    for stage, ordinal, st in spec.numbered_steps():
        user_id = None
        if st.user_email:
            user_id = await s.scalar(select(User.id).where(func.lower(User.email) == st.user_email.lower()))
            if user_id is None:
                raise TemplateConfigError(f"Шаблон {spec.code}: користувача {st.user_email} не знайдено")
        steps.append(RouteTemplateStepIn(
            stage_no=stage, ordinal=ordinal, step_kind=st.kind, assignee_rule=st.rule,
            assignee_user_id=user_id, assignee_org_unit_id=await _unit_id(s, st.unit, spec.code),
            position_code=st.position_code, role=st.role, deadline_hours=st.deadline_hours,
            is_mandatory=st.is_mandatory, requires_kep=st.requires_kep))
    try:
        return RouteTemplateCreate(code=spec.code, name_uk=spec.name_uk, description=spec.description,
                                   document_type_id=type_id,
                                   org_unit_id=await _unit_id(s, spec.org_unit, spec.code), steps=steps)
    except ValueError as exc:
        raise TemplateConfigError(f"Шаблон {spec.code}: {exc}") from exc


async def sync_templates(s: AsyncSession, catalog: TemplateCatalog, created_by_id: uuid.UUID) -> list[SyncResult]:
    """Кожен шаблон у власному savepoint: помилка в одному (немає підрозділу) не зупиняє інші."""
    results = []
    for spec in catalog:
        try:
            async with s.begin_nested():
                results.append(await _sync_one(s, spec, created_by_id))
        except TemplateConfigError as exc:
            results.append(SyncResult(spec.code, 0, "error", exc.message))
    return results


async def _sync_one(s: AsyncSession, spec: TemplateSpec, created_by_id: uuid.UUID) -> SyncResult:
    dto = await build_dto(s, spec)
    latest = (await s.scalars(
        select(RouteTemplate).options(selectinload(RouteTemplate.steps))
        .where(RouteTemplate.code == dto.code).order_by(RouteTemplate.version.desc()).limit(1)
        .with_for_update()
    )).first()
    same = latest is not None and (
        latest.name_uk, latest.description, latest.document_type_id, latest.org_unit_id
    ) == (dto.name_uk, dto.description, dto.document_type_id, dto.org_unit_id) and (
        _structure(latest.steps) == _structure(dto.steps))
    if same:
        if not latest.is_active:
            latest.is_active = True
        return SyncResult(dto.code, latest.version, "unchanged")
    version = (latest.version + 1) if latest else 1
    await s.execute(update(RouteTemplate).where(RouteTemplate.code == dto.code, RouteTemplate.is_active)
                    .values(is_active=False))
    tpl = RouteTemplate(code=dto.code, version=version, name_uk=dto.name_uk, description=dto.description,
                        document_type_id=dto.document_type_id, org_unit_id=dto.org_unit_id,
                        created_by_id=created_by_id)
    s.add(tpl)
    await s.flush()
    s.add_all(RouteTemplateStep(template_id=tpl.id, **st.model_dump()) for st in dto.steps)
    await s.flush()
    return SyncResult(dto.code, version, "updated" if latest else "created")
