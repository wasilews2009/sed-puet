"""Визначення посадових осіб для кроків маршруту.

Маршрут описує *посади* (завідувач кафедри автора, проректор за напрямом, керівник
бухгалтерії…), а резолвер на конкретну дату перетворює їх на людей з `user_roles`
(чинні призначення, т.в.о. включно) і застосовує заміщення з `delegations`.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime

from sed_db.enums import AssigneeRule, OrgUnitKind, UserRole

from ..ports import OrgDirectory
from .errors import AssigneeUnresolved
from .models import DelegationInfo, DocumentContext, Holder, PlannedStep, StepRule

# Яка роль означає «керівник» для підрозділу певного виду.
HEAD_ROLE_BY_KIND: dict[OrgUnitKind, UserRole] = {
    OrgUnitKind.DEPARTMENT: UserRole.DEPARTMENT_HEAD,
    OrgUnitKind.INSTITUTE: UserRole.INSTITUTE_DIRECTOR,
}
MAX_DELEGATION_HOPS = 3


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("’", "'")).strip().casefold()


class AssigneeResolver:
    def __init__(self, directory: OrgDirectory) -> None:
        self.directory = directory

    async def unit_heads(self, unit_id: uuid.UUID, on: date) -> list[Holder]:
        chain = await self.directory.unit_chain(unit_id)
        if not chain:
            return []
        kind = chain[0].kind
        if kind is OrgUnitKind.UNIVERSITY:
            return await self.directory.role_holders(UserRole.RECTOR, None, on)
        return await self.directory.role_holders(HEAD_ROLE_BY_KIND.get(kind, UserRole.UNIT_HEAD), unit_id, on)

    async def candidates(self, rule: StepRule, doc: DocumentContext, on: date,
                         *, ignore_scope: bool = False) -> list[Holder]:
        """Усі люди, які за правилом можуть бути учасником кроку."""
        r = rule.rule
        if r is AssigneeRule.SPECIFIC_USER:
            h = await self.directory.user(rule.user_id) if rule.user_id else None
            return [h] if h else []
        if r is AssigneeRule.UNIT_HEAD:
            return await self.unit_heads(rule.unit_id, on) if rule.unit_id else []
        if r is AssigneeRule.AUTHOR_UNIT_HEAD:
            return await self.unit_heads(doc.org_unit_id, on)
        if r is AssigneeRule.POSITION_IN_UNIT:
            return await self.directory.position_holders(rule.unit_id, rule.position_code, on)  # type: ignore[arg-type]
        if r is AssigneeRule.ROLE:
            assert rule.role is not None
            holders = await self.directory.role_holders(rule.role, rule.unit_id, on,
                                                        any_unit=rule.unit_id is None)
            scope = rule.extras.role_scope
            if scope and not ignore_scope:
                holders = [h for h in holders if _norm(h.scope_note) == _norm(scope)]
            return holders
        if r is AssigneeRule.AUTHOR_UNIT_ROLE:
            assert rule.role is not None
            for unit in await self.directory.unit_chain(doc.org_unit_id):
                holders = await self.directory.role_holders(rule.role, unit.id, on)
                if holders:
                    return holders
            return []
        raise AssigneeUnresolved(f"Невідоме правило призначення {r}")

    async def plan_step(self, rule: StepRule, doc: DocumentContext, on: date,
                        override: uuid.UUID | None = None) -> PlannedStep:
        title = rule.extras.title_uk
        if override is not None:
            # Ініціатор може обрати лише з тих, хто за правилом має право візувати
            # (напр. одного з проректорів), а не довільну людину.
            pool = await self.candidates(rule, doc, on, ignore_scope=True)
            chosen = next((h for h in pool if h.user_id == override), None)
            if chosen is None:
                raise AssigneeUnresolved(
                    f"Обрана особа не обіймає посаду «{title}»",
                    stage_no=rule.stage_no, ordinal=rule.ordinal)
        else:
            found = await self.candidates(rule, doc, on)
            if not found:
                raise AssigneeUnresolved(
                    f"Посада «{title}» вакантна або не знайдена в оргструктурі: призначте т.в.о. "
                    "або оберіть учасника вручну", stage_no=rule.stage_no, ordinal=rule.ordinal)
            if len({h.user_id for h in found}) > 1:
                raise AssigneeUnresolved(
                    f"На посаду «{title}» знайдено кількох осіб — оберіть учасника",
                    stage_no=rule.stage_no, ordinal=rule.ordinal,
                    candidates=[{"user_id": str(h.user_id), "name": h.display_name,
                                 "scope": h.scope_note} for h in found])
            chosen = found[0]
        skip = rule.extras.skip_if_author and chosen.user_id == doc.author_id
        return PlannedStep(
            rule=rule, assignee=chosen, acting_role=chosen.role or rule.role, will_skip=skip,
            skip_reason="Автор документа сам обіймає цю посаду" if skip else None)

    async def still_holds(self, rule: StepRule, doc: DocumentContext, user_id: uuid.UUID, on: date) -> bool:
        pool = await self.candidates(rule, doc, on, ignore_scope=True)
        return any(h.user_id == user_id for h in pool)

    async def effective_assignee(
        self, user_id: uuid.UUID, document_type_id: uuid.UUID, at: datetime,
    ) -> tuple[uuid.UUID, DelegationInfo | None]:
        """Хто фактично візує: сама людина або її заступник (ланцюжок заміщень ≤ 3, без циклів)."""
        seen = {user_id}
        current, last = user_id, None
        for _ in range(MAX_DELEGATION_HOPS):
            d = await self.directory.active_delegation(current, document_type_id, at)
            if d is None or d.to_user_id in seen:
                break
            deputy = await self.directory.user(d.to_user_id)
            if deputy is None:  # заступник звільнений/деактивований — заміщення ігноруємо
                break
            seen.add(d.to_user_id)
            current, last = d.to_user_id, d
        return current, last
