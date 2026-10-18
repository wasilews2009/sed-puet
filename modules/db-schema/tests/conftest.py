"""Інтеграційні тести на живому PostgreSQL 16 + pgvector.

    export SED_TEST_DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/sed_test
    pytest -q tests

БД перестворюється, схема накочується міграцією Alembic (тестується саме міграція).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from sed_db.enums import DocumentDirection, OrgUnitKind
from sed_db.models import DocumentType, OrgUnit, RegistrationJournal, User

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = make_url(os.environ["SED_TEST_DATABASE_URL"])
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()

    os.environ["SED_DATABASE_URL"] = url.render_as_string(hide_password=False)
    cfg = Config(str(ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")

    eng = create_engine(url)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    """Кожен тест — у власній транзакції, яка відкочується."""
    with engine.connect() as conn:
        trans = conn.begin()
        with Session(bind=conn, join_transaction_mode="create_savepoint") as s:
            yield s
        trans.rollback()


def act_as(session: Session, user_id: uuid.UUID | None, actor_type: str = "user") -> None:
    session.execute(
        text("SELECT set_config('sed.actor_user_id', :u, true), set_config('sed.actor_type', :t, true)"),
        {"u": str(user_id) if user_id else "", "t": actor_type},
    )


@pytest.fixture()
def world(session: Session) -> dict[str, object]:
    """Мінімальна оргструктура: університет, кафедра, автор, завкафедри, ректор."""
    uni = OrgUnit(kind=OrgUnitKind.UNIVERSITY, code="PUET", name_uk="Полтавський університет економіки і торгівлі")
    session.add(uni)
    session.flush()
    dept = OrgUnit(kind=OrgUnitKind.DEPARTMENT, code="K-EK", name_uk="Кафедра економіки", parent_id=uni.id)
    session.add(dept)

    def mk_user(last: str, first: str) -> User:
        u = User(keycloak_sub=uuid.uuid4(), email=f"{uuid.uuid4().hex[:8]}@puet.edu.ua",
                 last_name=last, first_name=first, primary_org_unit_id=dept.id)
        session.add(u)
        return u

    author, head, rector, operator = (
        mk_user("Петренко", "Олена"), mk_user("Коваль", "Андрій"),
        mk_user("Ректоренко", "Іван"), mk_user("Операторенко", "Марія"),
    )
    # Вид із seed-міграції 0002.
    order_type = session.scalars(select(DocumentType).where(DocumentType.code == "ORDER_MAIN")).one()
    journal = RegistrationJournal(code="ORD", name_uk="Журнал реєстрації наказів з основної діяльності",
                                  year=date.today().year, document_type_id=order_type.id,
                                  number_pattern="{seq}{suffix}", suffix="-од")
    session.add(journal)
    session.flush()
    return dict(uni=uni, dept=dept, author=author, head=head, rector=rector,
                operator=operator, order_type=order_type, journal=journal,
                direction=DocumentDirection.INTERNAL)
