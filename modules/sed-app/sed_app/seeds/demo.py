"""Демо-оргструктура і користувачі з усіма 9 ролями (лише для dev/стенду).

    python -m sed_app.seeds.demo load     # ідемпотентно; потрібен SED_APP_DATABASE_URL
    python -m sed_app.seeds.demo realm    # realm-export Keycloak з тими самими користувачами

Імена вигадані. Коди підрозділів BUH і PFV та напрям проректора збігаються з шаблоном
`approval-routing/templates/memo_procurement.toml`, тож службова записка на закупівлю
одразу маршрутизується: зав. кафедри → проректор → головний бухгалтер → ректор → ПФВ.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sed_db.enums import OrgUnitKind, UserRole
from sed_db.models import OrgUnit, StaffAssignment, User, UserRoleAssignment
from sed_db.seeds import SYSTEM_USER_ID

_NS = uuid.UUID("6f1c3c1e-5b6a-4c55-9a0e-2f5d7b8e9a10")
DEMO_DOMAIN = "demo.puet.local"
VALID_FROM = date(2026, 9, 1)


@dataclass(frozen=True)
class DemoUnit:
    code: str
    kind: OrgUnitKind
    name: str
    short: str | None
    parent: str | None


@dataclass(frozen=True)
class DemoUser:
    login: str
    first: str
    last: str
    patronymic: str
    unit: str
    position: str
    roles: tuple[tuple[UserRole, str | None, str | None], ...]   # роль, код підрозділу, scope_note

    @property
    def email(self) -> str:
        return f"{self.login}@{DEMO_DOMAIN}"

    @property
    def keycloak_sub(self) -> uuid.UUID:
        return uuid.uuid5(_NS, self.email)


UNITS: tuple[DemoUnit, ...] = (
    DemoUnit("PUET", OrgUnitKind.UNIVERSITY, "Полтавський університет економіки і торгівлі", "ПУЕТ", None),
    DemoUnit("RECT", OrgUnitKind.RECTORATE, "Ректорат", None, "PUET"),
    DemoUnit("NNIDO", OrgUnitKind.INSTITUTE, "Навчально-науковий інститут денної освіти", "ННІ ДО", "PUET"),
    DemoUnit("KKNIT", OrgUnitKind.DEPARTMENT, "Кафедра комп'ютерних наук та інформаційних технологій",
             "Каф. КНІТ", "NNIDO"),
    DemoUnit("BUH", OrgUnitKind.DIVISION, "Бухгалтерія", None, "PUET"),
    DemoUnit("PFV", OrgUnitKind.DIVISION, "Планово-фінансовий відділ", "ПФВ", "PUET"),
    DemoUnit("ZAG", OrgUnitKind.DIVISION, "Загальний відділ (канцелярія)", None, "PUET"),
    DemoUnit("SSV", OrgUnitKind.COLLEGIAL_BODY, "Студентське самоврядування", None, "PUET"),
)

_VR_SCOPE = "з адміністративно-господарської роботи"

USERS: tuple[DemoUser, ...] = (
    DemoUser("rector", "Олександр", "Демченко", "Петрович", "RECT", "Ректор",
             ((UserRole.RECTOR, None, None),)),
    DemoUser("vicerector", "Ірина", "Савчук", "Олегівна", "RECT", "Проректор з адміністративно-господарської роботи",
             ((UserRole.VICE_RECTOR, None, _VR_SCOPE),)),
    DemoUser("director", "Віктор", "Гнатюк", "Іванович", "NNIDO", "Директор інституту",
             ((UserRole.INSTITUTE_DIRECTOR, "NNIDO", None),)),
    DemoUser("head.kknit", "Наталія", "Кравець", "Василівна", "KKNIT", "Завідувач кафедри",
             ((UserRole.DEPARTMENT_HEAD, "KKNIT", None), (UserRole.LECTURER, "KKNIT", None))),
    DemoUser("lecturer", "Андрій", "Мельник", "Сергійович", "KKNIT", "Доцент",
             ((UserRole.LECTURER, "KKNIT", None),)),
    DemoUser("student", "Марія", "Бондар", "Андріївна", "KKNIT", "Здобувач вищої освіти",
             ((UserRole.STUDENT, "KKNIT", "група КН-21"),)),
    DemoUser("studgov", "Денис", "Ткачук", "Миколайович", "SSV", "Голова студентського самоврядування",
             ((UserRole.STUDENT_GOVERNMENT_HEAD, None, None), (UserRole.STUDENT, "KKNIT", "група КН-31"))),
    DemoUser("buh", "Людмила", "Романенко", "Григорівна", "BUH", "Головний бухгалтер",
             ((UserRole.UNIT_HEAD, "BUH", None),)),
    DemoUser("pfv", "Тетяна", "Литвин", "Юріївна", "PFV", "Начальник планово-фінансового відділу",
             ((UserRole.UNIT_HEAD, "PFV", None),)),
    DemoUser("clerk", "Оксана", "Павленко", "Віталіївна", "ZAG", "Діловод загального відділу",
             ((UserRole.STAFF, "ZAG", None),)),
)


async def load(s: AsyncSession) -> dict[str, int]:
    """Ідемпотентно: підрозділи за кодом, користувачі за e-mail, ролі за (user, role, unit)."""
    units: dict[str, uuid.UUID] = {}
    created = {"units": 0, "users": 0, "roles": 0}
    for u in UNITS:
        unit = await s.scalar(select(OrgUnit).where(OrgUnit.code == u.code))
        if unit is None:
            unit = OrgUnit(code=u.code, kind=u.kind, name_uk=u.name, short_name_uk=u.short,
                           parent_id=units[u.parent] if u.parent else None, valid_from=VALID_FROM)
            s.add(unit)
            await s.flush()
            created["units"] += 1
        units[u.code] = unit.id

    for d in USERS:
        user = await s.scalar(select(User).where(User.email == d.email))
        if user is None:
            user = User(keycloak_sub=d.keycloak_sub, email=d.email, first_name=d.first, last_name=d.last,
                        patronymic=d.patronymic, primary_org_unit_id=units[d.unit])
            s.add(user)
            await s.flush()
            s.add(StaffAssignment(user_id=user.id, org_unit_id=units[d.unit], position_code=d.login.upper(),
                                  position_title_uk=d.position, valid_from=VALID_FROM,
                                  is_head=any(r[0] in (UserRole.UNIT_HEAD, UserRole.DEPARTMENT_HEAD,
                                                       UserRole.INSTITUTE_DIRECTOR, UserRole.RECTOR)
                                              for r in d.roles)))
            created["users"] += 1
        for role, unit_code, scope in d.roles:
            unit_id = units[unit_code] if unit_code else None
            exists_ = await s.scalar(select(UserRoleAssignment.id).where(
                UserRoleAssignment.user_id == user.id, UserRoleAssignment.role == role,
                UserRoleAssignment.org_unit_id.is_(None) if unit_id is None
                else UserRoleAssignment.org_unit_id == unit_id))
            if exists_ is None:
                s.add(UserRoleAssignment(user_id=user.id, role=role, org_unit_id=unit_id, scope_note=scope,
                                         valid_from=VALID_FROM, basis="Демо-дані стенду СЕД",
                                         granted_by_id=SYSTEM_USER_ID))
                created["roles"] += 1
        await s.flush()
    return created


async def sync_route_templates(s: AsyncSession) -> list[str]:
    """Шаблони маршрутів посилаються на коди підрозділів: синхронізуємо після оргструктури."""
    try:
        from sed_routing.adapters.template_sync import sync_templates
        from sed_routing.config import Settings as RoutingSettings
        from sed_routing.domain.templates import TemplateCatalog
    except ImportError:
        return ["модуль sed_routing не встановлено — шаблони маршрутів пропущено"]
    catalog = TemplateCatalog.from_dir(RoutingSettings(environment="development").templates_dir)
    return [f"{r.code} v{r.version}: {r.action}{' — ' + r.error if r.error else ''}"
            for r in await sync_templates(s, catalog, SYSTEM_USER_ID)]


def keycloak_realm(password: str = "demo") -> dict[str, object]:
    """Realm `puet`: лише 9 ролей СЕД, публічний клієнт веб-інтерфейсу з PKCE, audience `sed-api`.
    Нормативний корпус змінюють ректор і проректор (`SED_RAG_CORPUS_ADMIN_ROLES` у docker-compose)."""
    from sed_db.enums import LABELS_UK
    roles = [{"name": r.value, "description": LABELS_UK[r]} for r in UserRole]
    return {
        "realm": "puet", "enabled": True, "displayName": "ПУЕТ — СЕД", "sslRequired": "external",
        "accessTokenLifespan": 900, "bruteForceProtected": True,
        "roles": {"realm": roles},
        "clients": [{
            "clientId": "sed-web", "name": "СЕД ПУЕТ — веб-інтерфейс", "publicClient": True,
            "standardFlowEnabled": True, "directAccessGrantsEnabled": False, "implicitFlowEnabled": False,
            "redirectUris": ["http://localhost:8080/*"], "webOrigins": ["http://localhost:8080"],
            "attributes": {"pkce.code.challenge.method": "S256", "post.logout.redirect.uris": "http://localhost:8080/*"},
            "protocolMappers": [{
                "name": "audience-sed-api", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
                "config": {"included.custom.audience": "sed-api", "access.token.claim": "true",
                           "id.token.claim": "false"},
            }],
        }],
        "users": [{
            "id": str(d.keycloak_sub), "username": d.login, "email": d.email, "emailVerified": True,
            "firstName": d.first, "lastName": d.last, "enabled": True,
            "credentials": [{"type": "password", "value": password, "temporary": True}],
            "realmRoles": sorted({r[0].value for r in d.roles}),
        } for d in USERS],
    }


async def _load_cli(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s, s.begin():
            await s.execute(text("SELECT set_config('sed.actor_user_id', :u, true)"), {"u": str(SYSTEM_USER_ID)})
            created = await load(s)
            templates = await sync_route_templates(s)
    finally:
        await engine.dispose()
    print(f"Підрозділів створено: {created['units']}, користувачів: {created['users']}, ролей: {created['roles']}")
    for line in templates:
        print(f"Шаблон маршруту {line}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sed_app.seeds.demo")
    p.add_argument("command", choices=["load", "realm"])
    p.add_argument("--password", default="demo", help="Тимчасовий пароль демо-користувачів у Keycloak")
    args = p.parse_args(argv)
    if args.command == "realm":
        json.dump(keycloak_realm(args.password), sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0
    from ..config import Settings
    asyncio.run(_load_cli(Settings(environment="development", auth_mode="dev").database_url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
