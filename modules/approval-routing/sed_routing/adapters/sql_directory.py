"""Оргструктура з PostgreSQL (`org_units`, `user_roles`, `staff_assignments`, `delegations`)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sed_db.enums import UserRole
from sed_db.models import Delegation, OrgUnit, StaffAssignment, User, UserRoleAssignment

from ..domain.models import DelegationInfo, Holder, UnitInfo

MAX_DEPTH = 10  # захист від циклу в ієрархії підрозділів


def _display(u: User) -> str:
    return f"{u.first_name} {u.last_name.upper()}"


def _valid_on(cls: type, on: date):  # noqa: ANN202
    return and_(cls.valid_from <= on, or_(cls.valid_to.is_(None), cls.valid_to > on))


class SqlOrgDirectory:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def unit_chain(self, unit_id: uuid.UUID) -> list[UnitInfo]:
        chain: list[UnitInfo] = []
        current: uuid.UUID | None = unit_id
        while current is not None and len(chain) < MAX_DEPTH:
            u = await self.s.get(OrgUnit, current)
            if u is None or any(c.id == u.id for c in chain):
                break
            chain.append(UnitInfo(u.id, u.kind, u.code, u.name_uk, u.parent_id))
            current = u.parent_id
        return chain

    async def role_holders(self, role: UserRole, unit_id: uuid.UUID | None, on: date,
                           *, any_unit: bool = False) -> list[Holder]:
        q = (select(UserRoleAssignment, User)
             .join(User, User.id == UserRoleAssignment.user_id)
             .where(UserRoleAssignment.role == role, User.is_active, _valid_on(UserRoleAssignment, on))
             .order_by(UserRoleAssignment.is_acting, User.last_name))
        if not any_unit:
            q = q.where(UserRoleAssignment.org_unit_id.is_(None) if unit_id is None
                        else UserRoleAssignment.org_unit_id == unit_id)
        rows = (await self.s.execute(q)).all()
        return [Holder(u.id, _display(u), r.role, r.org_unit_id, r.is_acting, r.scope_note) for r, u in rows]

    async def position_holders(self, unit_id: uuid.UUID, position_code: str, on: date) -> list[Holder]:
        rows = (await self.s.execute(
            select(StaffAssignment, User).join(User, User.id == StaffAssignment.user_id)
            .where(StaffAssignment.org_unit_id == unit_id, StaffAssignment.position_code == position_code,
                   User.is_active, _valid_on(StaffAssignment, on))
        )).all()
        return [Holder(u.id, _display(u), None, a.org_unit_id, a.is_acting, a.position_title_uk) for a, u in rows]

    async def user(self, user_id: uuid.UUID) -> Holder | None:
        u = await self.s.get(User, user_id)
        return Holder(u.id, _display(u), org_unit_id=u.primary_org_unit_id) if u and u.is_active else None

    async def active_delegation(self, user_id: uuid.UUID, document_type_id: uuid.UUID,
                                at: datetime) -> DelegationInfo | None:
        d = (await self.s.scalars(
            select(Delegation)
            .where(Delegation.from_user_id == user_id, Delegation.revoked_at.is_(None),
                   Delegation.valid_from <= at, Delegation.valid_to > at,
                   or_(Delegation.document_type_id.is_(None), Delegation.document_type_id == document_type_id))
            # Заміщення саме за цим видом документа важливіше за загальне.
            .order_by(Delegation.document_type_id.is_(None), Delegation.valid_from.desc())
            .limit(1)
        )).first()
        return DelegationInfo(d.id, d.from_user_id, d.to_user_id, d.valid_to) if d else None
