"""Спільні фікстури: імітаційний КНЕДП, верифікатор, жива БД PostgreSQL 16 (+pgvector).

    export SED_TEST_DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/sed_routing_test
    pytest -q

Без SED_TEST_DATABASE_URL інтеграційні тести пропускаються; чисті (домен, КЕП) йдуть завжди.
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.dont_write_bytecode = True   # не залишати __pycache__ у спільній папці (зокрема в ../db-schema)
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent / "db-schema")]

from sed_db.enums import (  # noqa: E402
    DocumentDirection,
    DocumentStatus,
    OrgUnitKind,
    UserRole,
    VersionSource,
)
from sed_routing.adapters.signature_store import InMemorySignatureStore  # noqa: E402
from sed_routing.domain.templates import TemplateCatalog  # noqa: E402
from sed_routing.kep.mock import MockKey, MockKnedp  # noqa: E402
from sed_routing.kep.models import PemTrustStore, StaticCertificateBinding, TaxIdHasher  # noqa: E402
from sed_routing.kep.verifier import CadesVerifier  # noqa: E402

HMAC_SECRET = b"test-secret-for-tax-id-hmac-32-bytes!!"
VICE_RECTOR_AHR = "з адміністративно-господарської роботи"
VICE_RECTOR_NPR = "з науково-педагогічної роботи"


@pytest.fixture(scope="session")
def knedp() -> MockKnedp:
    return MockKnedp()


@pytest.fixture(scope="session")
def hasher() -> TaxIdHasher:
    return TaxIdHasher(HMAC_SECRET)


@pytest.fixture(scope="session")
def binding() -> StaticCertificateBinding:
    return StaticCertificateBinding()


@pytest.fixture(scope="session")
def verifier(knedp: MockKnedp, binding: StaticCertificateBinding, hasher: TaxIdHasher) -> CadesVerifier:
    return CadesVerifier(PemTrustStore.from_pem(knedp.ca_pem), knedp, binding, hasher, allow_mock=True)


@pytest.fixture(scope="session")
def catalog() -> TemplateCatalog:
    return TemplateCatalog.from_dir(ROOT / "templates")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- live DB

@dataclass
class World:
    """Оргструктура ПУЕТ у мініатюрі + ключі КЕП кожного учасника."""

    ids: dict[str, uuid.UUID] = field(default_factory=dict)
    keys: dict[str, MockKey] = field(default_factory=dict)
    subs: dict[str, uuid.UUID] = field(default_factory=dict)
    memo_type_id: uuid.UUID | None = None
    institute_id: uuid.UUID | None = None


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("SED_TEST_DATABASE_URL")
    if not url:
        pytest.skip("SED_TEST_DATABASE_URL не задано — інтеграційні тести з PostgreSQL пропущено")
    return url


@pytest.fixture(scope="session")
async def engine(database_url: str):  # noqa: ANN201
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    url = make_url(database_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()
    os.environ["SED_DATABASE_URL"] = url.render_as_string(hide_password=False)
    command.upgrade(Config(str(ROOT.parent / "db-schema" / "alembic.ini")), "head")
    # Запит на зміну схеми (ключі КЕП, внутрішній ЦСК) — поки його не перенесено в ../db-schema.
    sync = create_engine(url)
    with sync.begin() as conn:
        conn.exec_driver_sql((ROOT / "schema_proposal" / "0003_kep_keys.sql").read_text(encoding="utf-8"))
    sync.dispose()
    eng = create_async_engine(url)
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
def sessionmaker(engine):  # noqa: ANN001, ANN201
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(engine, expire_on_commit=False)


async def as_system(s) -> None:  # noqa: ANN001
    from sqlalchemy import text

    from sed_db.seeds import SYSTEM_USER_ID

    await s.execute(text("SELECT set_config('sed.actor_user_id', :u, true)"), {"u": str(SYSTEM_USER_ID)})


async def add_user(s, w: World, key: str, last: str, first: str, unit: uuid.UUID | None,  # noqa: ANN001
                   knedp: MockKnedp, binding: StaticCertificateBinding, hasher: TaxIdHasher,
                   role: UserRole | None = None, role_unit: uuid.UUID | None = None, scope: str | None = None,
                   is_acting: bool = False) -> uuid.UUID:
    from sed_db.models import User, UserRoleAssignment

    sub = uuid.uuid4()
    u = User(keycloak_sub=sub, email=f"{key}-{uuid.uuid4().hex[:6]}@puet.edu.ua", last_name=last,
             first_name=first, primary_org_unit_id=unit)
    s.add(u)
    await s.flush()
    if role is not None:
        s.add(UserRoleAssignment(user_id=u.id, role=role, org_unit_id=role_unit, scope_note=scope,
                                 is_acting=is_acting, valid_from=date(2020, 1, 1)))
        await s.flush()
    tax_id = f"{u.id.int % 10**10:010d}"
    w.keys[key] = knedp.issue(f"{first} {last.upper()}", tax_id)
    binding.bind(u.id, hasher(tax_id))
    w.ids[key], w.subs[key] = u.id, sub
    return u.id


@pytest.fixture(scope="session")
async def world(sessionmaker, knedp, binding, hasher, catalog) -> World:  # noqa: ANN001
    """Університет → інститут → кафедра; бухгалтерія (BUH), ПФВ (PFV); ректор, два проректори."""
    from sqlalchemy import select

    from sed_db.models import DocumentType, OrgUnit
    from sed_db.seeds import SYSTEM_USER_ID
    from sed_routing.adapters.template_sync import sync_templates

    w = World()
    async with sessionmaker() as s, s.begin():
        await as_system(s)
        uni = OrgUnit(kind=OrgUnitKind.UNIVERSITY, code="PUET", name_uk="Полтавський університет економіки і торгівлі")
        s.add(uni)
        await s.flush()
        inst = OrgUnit(kind=OrgUnitKind.INSTITUTE, code="NNI-EUP", name_uk="ННІ економіки, управління та права",
                       parent_id=uni.id)
        buh = OrgUnit(kind=OrgUnitKind.DIVISION, code="BUH", name_uk="Бухгалтерія", parent_id=uni.id)
        pfv = OrgUnit(kind=OrgUnitKind.DIVISION, code="PFV", name_uk="Планово-фінансовий відділ", parent_id=uni.id)
        s.add_all([inst, buh, pfv])
        await s.flush()
        w.institute_id = inst.id
        w.ids.update(uni=uni.id, buh=buh.id, pfv=pfv.id)
        a = dict(knedp=knedp, binding=binding, hasher=hasher)
        await add_user(s, w, "rector", "Ректоренко", "Іван", uni.id, role=UserRole.RECTOR, **a)
        await add_user(s, w, "vr_ahr", "Господаренко", "Петро", uni.id, role=UserRole.VICE_RECTOR,
                       scope=VICE_RECTOR_AHR, **a)
        await add_user(s, w, "vr_npr", "Науменко", "Ольга", uni.id, role=UserRole.VICE_RECTOR,
                       scope=VICE_RECTOR_NPR, **a)
        await add_user(s, w, "chief_acc", "Бухгалтеренко", "Надія", buh.id, role=UserRole.UNIT_HEAD,
                       role_unit=buh.id, **a)
        await add_user(s, w, "pfv_head", "Фінансенко", "Марина", pfv.id, role=UserRole.UNIT_HEAD,
                       role_unit=pfv.id, **a)
        await add_user(s, w, "outsider", "Сторонній", "Олег", uni.id, **a)
        w.memo_type_id = await s.scalar(select(DocumentType.id).where(DocumentType.code == "MEMO"))
        results = await sync_templates(s, catalog, SYSTEM_USER_ID)
        assert [r.action for r in results] == ["created"], results
    return w


@dataclass
class Dept:
    id: uuid.UUID
    author: str
    head: str


@pytest.fixture()
async def dept(sessionmaker, world: World, knedp, binding, hasher) -> Dept:  # noqa: ANN001
    """Окрема кафедра на тест: автор-викладач і завідувач (тести не заважають один одному)."""
    from sed_db.models import OrgUnit

    tag = uuid.uuid4().hex[:6]
    async with sessionmaker() as s, s.begin():
        await as_system(s)
        d = OrgUnit(kind=OrgUnitKind.DEPARTMENT, code=f"K-{tag}", name_uk=f"Кафедра {tag}", parent_id=world.institute_id)
        s.add(d)
        await s.flush()
        a = dict(knedp=knedp, binding=binding, hasher=hasher)
        await add_user(s, world, f"author-{tag}", "Петренко", "Олена", d.id, role=UserRole.LECTURER,
                       role_unit=d.id, **a)
        await add_user(s, world, f"head-{tag}", "Коваль", "Андрій", d.id, role=UserRole.DEPARTMENT_HEAD,
                       role_unit=d.id, **a)
    return Dept(d.id, f"author-{tag}", f"head-{tag}")


async def make_memo(sessionmaker, world: World, unit_id: uuid.UUID, author_key: str,  # noqa: ANN001
                    content: bytes | None = None, title: str = "Про закупівлю мультимедійного проєктора") -> tuple[uuid.UUID, str]:
    from sed_db.enums import AccessLevel
    from sed_db.models import Document, DocumentVersion
    from sqlalchemy import text

    content = content or f"%PDF-1.7 memo {uuid.uuid4()}".encode()
    async with sessionmaker() as s, s.begin():
        await s.execute(text("SELECT set_config('sed.actor_user_id', :u, true)"), {"u": str(world.ids[author_key])})
        doc = Document(document_type_id=world.memo_type_id, direction=DocumentDirection.INTERNAL,
                       access_level=AccessLevel.INTERNAL, title=title, author_id=world.ids[author_key],
                       org_unit_id=unit_id, status=DocumentStatus.DRAFT)
        s.add(doc)
        await s.flush()
        ver = DocumentVersion(document_id=doc.id, version_number=1, source=VersionSource.EDITOR,
                              storage_bucket="sed-documents", storage_key=f"{doc.id}/v1.pdf",
                              file_name="sluzhbova.pdf", mime_type="application/pdf",
                              size_bytes=len(content), content_sha256=sha(content),
                              created_by_id=world.ids[author_key])
        s.add(ver)
        await s.flush()
        doc.current_version_id = ver.id
    return doc.id, sha(content)


@pytest.fixture()
def store() -> InMemorySignatureStore:
    return InMemorySignatureStore()


@pytest.fixture()
def service(sessionmaker, catalog, verifier, store):  # noqa: ANN001, ANN201
    from sed_routing.service import ApprovalService

    return ApprovalService(sessionmaker, catalog, verifier, store)


def caller(world: World, key: str, *roles: str):  # noqa: ANN201
    from sed_routing.service import Caller

    return Caller(keycloak_sub=world.subs[key], roles=frozenset(roles))


def now() -> datetime:
    return datetime.now(timezone.utc)
