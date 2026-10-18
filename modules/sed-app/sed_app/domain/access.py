"""Політика доступу (RBAC + ABAC) до документів СЕД. Чиста логіка без БД.

Хто бачить документ:

| Хто                                              | Що бачить                                        |
|--------------------------------------------------|--------------------------------------------------|
| ректор, проректор                                | усі документи                                    |
| автор                                            | свої документи                                   |
| учасник маршруту погодження                      | документи, які йому надіслано на візу/підпис     |
| завідувач кафедри, директор інституту, керівник  | усі документи свого підрозділу і вкладених       |
| викладач, співробітник                           | документи свого підрозділу з грифом «внутрішній» |
| будь-хто з обліковим записом                     | документи з грифом «публічна інформація»         |

Документи з грифами «обмежений доступ» і «персональні дані» рядовим членам підрозділу не
видно: лише автору, керівникам, учасникам маршруту і керівництву університету. Студент і голова
студентського самоврядування бачать лише власні, публічні і надіслані їм документи.

Та сама політика втілена в SQL-фільтрі списку (`adapters/sql_documents.visible_clause`);
тести перевіряють, що обидві реалізації дають однаковий результат.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from sed_db.enums import AccessLevel, DocumentStatus, UserRole

UNIVERSITY_WIDE_ROLES: frozenset[UserRole] = frozenset({UserRole.RECTOR, UserRole.VICE_RECTOR})
HEAD_ROLES: frozenset[UserRole] = frozenset(
    {UserRole.DEPARTMENT_HEAD, UserRole.INSTITUTE_DIRECTOR, UserRole.UNIT_HEAD})
MEMBER_ROLES: frozenset[UserRole] = frozenset({UserRole.LECTURER, UserRole.STAFF})
MEMBER_VISIBLE_LEVELS: frozenset[AccessLevel] = frozenset({AccessLevel.PUBLIC, AccessLevel.INTERNAL})

# Після цих статусів документ не редагується і не відправляється на погодження повторно.
FINAL_STATUSES: frozenset[DocumentStatus] = frozenset({
    DocumentStatus.SIGNED, DocumentStatus.REGISTERED, DocumentStatus.IN_EXECUTION,
    DocumentStatus.EXECUTED, DocumentStatus.ARCHIVED, DocumentStatus.CANCELLED,
})


@dataclass(frozen=True)
class RoleGrant:
    """Чинне призначення з `user_roles`."""

    role: UserRole
    org_unit_id: uuid.UUID | None
    scope_note: str | None = None
    is_acting: bool = False


@dataclass(frozen=True)
class Actor:
    """Автентифікований користувач СЕД з чинними ролями (перетин ролей токена і БД)."""

    user_id: uuid.UUID
    keycloak_sub: uuid.UUID
    display_name: str
    email: str
    primary_org_unit_id: uuid.UUID | None
    grants: tuple[RoleGrant, ...] = ()
    realm_roles: frozenset[str] = frozenset()

    @property
    def roles(self) -> frozenset[UserRole]:
        return frozenset(g.role for g in self.grants)

    @property
    def is_university_wide(self) -> bool:
        return bool(self.roles & UNIVERSITY_WIDE_ROLES)

    @property
    def headed_units(self) -> frozenset[uuid.UUID]:
        """Підрозділи, якими людина керує (без вкладених — їх додає `OrgTree.subtree`)."""
        return frozenset(g.org_unit_id for g in self.grants if g.role in HEAD_ROLES and g.org_unit_id)

    @property
    def member_units(self) -> frozenset[uuid.UUID]:
        """Підрозділи, де людина працює викладачем/співробітником (не студентом)."""
        units = {g.org_unit_id for g in self.grants if g.role in MEMBER_ROLES and g.org_unit_id}
        return frozenset(u for u in units if u is not None)

    @property
    def upload_units(self) -> frozenset[uuid.UUID]:
        """Де можна реєструвати документи (та сама умова, що в `sed_ingest` resolve_uploader)."""
        units = {g.org_unit_id for g in self.grants if g.org_unit_id is not None}
        if self.primary_org_unit_id:
            units.add(self.primary_org_unit_id)
        return frozenset(units)


@dataclass(frozen=True)
class OrgTree:
    """Ієрархія підрозділів: id → parent_id."""

    parents: Mapping[uuid.UUID, uuid.UUID | None] = field(default_factory=dict)

    def children(self) -> dict[uuid.UUID, list[uuid.UUID]]:
        out: dict[uuid.UUID, list[uuid.UUID]] = {}
        for unit, parent in self.parents.items():
            if parent is not None:
                out.setdefault(parent, []).append(unit)
        return out

    def subtree(self, roots: Iterable[uuid.UUID]) -> frozenset[uuid.UUID]:
        """Підрозділи і всі вкладені (захищено від циклів)."""
        kids = self.children()
        seen: set[uuid.UUID] = set()
        stack = list(roots)
        while stack:
            u = stack.pop()
            if u in seen:
                continue
            seen.add(u)
            stack.extend(kids.get(u, ()))
        return frozenset(seen)


@dataclass(frozen=True)
class DocumentFacts:
    """Атрибути документа, від яких залежить доступ."""

    id: uuid.UUID
    author_id: uuid.UUID
    org_unit_id: uuid.UUID
    access_level: AccessLevel
    status: DocumentStatus
    route_participant_ids: frozenset[uuid.UUID] = frozenset()


@dataclass(frozen=True)
class AccessScope:
    """Готові множини для SQL-фільтра: обчислюються один раз на запит."""

    user_id: uuid.UUID
    all_documents: bool
    managed_units: frozenset[uuid.UUID]
    member_units: frozenset[uuid.UUID]


def scope_for(actor: Actor, tree: OrgTree) -> AccessScope:
    return AccessScope(
        user_id=actor.user_id,
        all_documents=actor.is_university_wide,
        managed_units=tree.subtree(actor.headed_units),
        member_units=actor.member_units,
    )


def can_view(scope: AccessScope, doc: DocumentFacts) -> bool:
    if scope.all_documents or doc.author_id == scope.user_id:
        return True
    if doc.access_level is AccessLevel.PUBLIC or scope.user_id in doc.route_participant_ids:
        return True
    if doc.org_unit_id in scope.managed_units:
        return True
    return doc.org_unit_id in scope.member_units and doc.access_level in MEMBER_VISIBLE_LEVELS


def can_review(scope: AccessScope, doc: DocumentFacts) -> bool:
    """Ручна валідація реквізитів ШІ і розгляд зауважень комплаєнсу: автор, керівники підрозділу,
    співробітники/викладачі підрозділу, керівництво університету.
    Учасник маршруту лише візує — правити реквізити чужого документа він не може."""
    if doc.status in FINAL_STATUSES:
        return False
    if scope.all_documents or doc.author_id == scope.user_id or doc.org_unit_id in scope.managed_units:
        return True
    return doc.org_unit_id in scope.member_units and doc.access_level in MEMBER_VISIBLE_LEVELS


def can_start_route(scope: AccessScope, doc: DocumentFacts) -> bool:
    """Запустити погодження може автор або керівник підрозділу документа."""
    if doc.status not in (DocumentStatus.DRAFT, DocumentStatus.AI_REVIEW, DocumentStatus.REVISION_REQUIRED):
        return False
    return doc.author_id == scope.user_id or doc.org_unit_id in scope.managed_units or scope.all_documents
