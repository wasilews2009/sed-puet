"""Користувачі СЕД: актор запиту (картка + чинні ролі), перший вхід, оргструктура."""

from __future__ import annotations

import time
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sed_db.enums import UserRole
from sed_db.models import OrgUnit, User, UserRoleAssignment

from ..auth.tokens import AppPrincipal
from ..domain.access import Actor, OrgTree, RoleGrant
from ..domain.errors import Forbidden


def display_name(first: str, last: str) -> str:
    """«Ім'я ПРІЗВИЩЕ» (ДСТУ 4163:2020)."""
    return f"{first} {last.upper()}".strip()


def _active_roles(today: date):  # noqa: ANN202 — SQLAlchemy-вираз
    return (UserRoleAssignment.valid_from <= today) & or_(
        UserRoleAssignment.valid_to.is_(None), UserRoleAssignment.valid_to > today)


class SqlUserDirectory:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], *, require_token_roles: bool,
                 jit_provisioning: bool, tree_ttl_s: int = 60) -> None:
        self.sessionmaker = sessionmaker
        self.require_token_roles = require_token_roles
        self.jit_provisioning = jit_provisioning
        self.tree_ttl_s = tree_ttl_s
        self._tree: OrgTree | None = None
        self._tree_loaded = 0.0

    async def actor(self, principal: AppPrincipal) -> Actor:
        today = date.today()
        async with self.sessionmaker() as s:
            user = await s.scalar(select(User).where(User.keycloak_sub == principal.sub))
            if user is None:
                user = await self._provision(s, principal)
            if not user.is_active:
                raise Forbidden("Обліковий запис у СЕД деактивовано")
            rows = (await s.execute(
                select(UserRoleAssignment.role, UserRoleAssignment.org_unit_id, UserRoleAssignment.scope_note,
                       UserRoleAssignment.is_acting)
                .where(UserRoleAssignment.user_id == user.id, _active_roles(today))
                .order_by(UserRoleAssignment.role)
            )).all()
        grants = tuple(
            RoleGrant(role=r.role, org_unit_id=r.org_unit_id, scope_note=r.scope_note, is_acting=r.is_acting)
            for r in rows
            # Роль, відкликана в Keycloak, перестає діяти одразу, без очікування синхронізації БД.
            if not self.require_token_roles or r.role.value in principal.roles
        )
        return Actor(
            user_id=user.id, keycloak_sub=user.keycloak_sub,
            display_name=display_name(user.first_name, user.last_name), email=user.email,
            primary_org_unit_id=user.primary_org_unit_id, grants=grants, realm_roles=principal.roles,
        )

    async def _provision(self, s: AsyncSession, p: AppPrincipal) -> User:
        if not self.jit_provisioning or not p.email:
            raise Forbidden("Обліковий запис не зареєстровано в СЕД. Зверніться до адміністратора")
        user = User(
            keycloak_sub=p.sub, email=p.email.lower(),
            first_name=(p.given_name or p.username or "—")[:128], last_name=(p.family_name or "—")[:128],
            last_synced_at=datetime.now(timezone.utc),
        )
        s.add(user)
        try:
            await s.commit()
        except IntegrityError:          # паралельний перший вхід або e-mail уже зайнятий
            await s.rollback()
            existing = await s.scalar(select(User).where(User.keycloak_sub == p.sub))
            if existing is None:
                raise Forbidden("Обліковий запис з цим e-mail уже існує в СЕД. Зверніться до адміністратора") from None
            return existing
        return user

    async def org_tree(self) -> OrgTree:
        if self._tree is None or time.monotonic() - self._tree_loaded > self.tree_ttl_s:
            async with self.sessionmaker() as s:
                rows = (await s.execute(select(OrgUnit.id, OrgUnit.parent_id))).all()
            self._tree = OrgTree({r.id: r.parent_id for r in rows})
            self._tree_loaded = time.monotonic()
        return self._tree

    async def org_units(self) -> list[OrgUnit]:
        async with self.sessionmaker() as s:
            return list((await s.scalars(
                select(OrgUnit).where(OrgUnit.is_active.is_(True)).order_by(OrgUnit.name_uk))).all())

    # --- dev-стенд ------------------------------------------------------------------

    async def dev_users(self) -> list[tuple[User, list[UserRole]]]:
        """Активні користувачі з ролями — вибір демо-користувача на сторінці входу (лише dev)."""
        today = date.today()
        async with self.sessionmaker() as s:
            users = (await s.scalars(select(User).where(User.is_active.is_(True))
                                     .order_by(User.last_name, User.first_name))).all()
            roles = (await s.execute(select(UserRoleAssignment.user_id, UserRoleAssignment.role)
                                     .where(_active_roles(today)))).all()
        by_user: dict[uuid.UUID, list[UserRole]] = {}
        for r in roles:
            by_user.setdefault(r.user_id, []).append(r.role)
        return [(u, sorted(by_user.get(u.id, []))) for u in users]

    async def dev_user(self, user_id: uuid.UUID) -> tuple[User, list[UserRole]] | None:
        for u, roles in await self.dev_users():
            if u.id == user_id:
                return u, roles
        return None
