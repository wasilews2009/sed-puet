"""Підготовка БД перед стартом застосунку (сервіс `migrate` у docker-compose).

1. `alembic upgrade head` зі схеми `../db-schema`;
2. DDL ключів КЕП з `../approval-routing/schema_proposal/0003_kep_keys.sql`, поки власник схеми
   не перенесе його в міграцію Alembic (див. README → «Що потрібно від модулів»);
3. за `--demo` — демо-підрозділи, 10 користувачів і шаблони маршрутів.

    python -m sed_app.cli.migrate [--demo]

URL бере з `SED_DATABASE_URL` (так само, як Alembic у db-schema).
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from sqlalchemy import create_engine, text

MODULES = Path(__file__).resolve().parents[3]
KEP_DDL = MODULES / "approval-routing" / "schema_proposal" / "0003_kep_keys.sql"


def upgrade(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(MODULES / "db-schema" / "alembic.ini"))
    cfg.set_main_option("script_location", str(MODULES / "db-schema" / "migrations"))
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            if conn.scalar(text("SELECT to_regclass('public.user_kep_identities')")) is None and KEP_DDL.exists():
                conn.exec_driver_sql(KEP_DDL.read_text(encoding="utf-8"))
                print("Застосовано", KEP_DDL.name)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sed_app.cli.migrate")
    p.add_argument("--demo", action="store_true", help="Завантажити демо-дані (лише для стенду)")
    args = p.parse_args(argv)
    url = os.environ["SED_DATABASE_URL"]
    upgrade(url)
    if args.demo:
        from ..seeds.demo import _load_cli
        asyncio.run(_load_cli(url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
