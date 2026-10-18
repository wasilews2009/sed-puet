"""Командний рядок: перевірка і синхронізація шаблонів маршрутів.

    python -m sed_routing.cli check-templates [--dir templates]
    python -m sed_routing.cli sync-templates  [--dir templates]   # потрібен SED_ROUTING_DATABASE_URL
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .config import Settings
from .domain.errors import TemplateConfigError
from .domain.templates import TemplateCatalog


async def _sync(directory: Path, database_url: str) -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sed_db.seeds import SYSTEM_USER_ID

    from .adapters.template_sync import sync_templates

    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine)() as s, s.begin():
            results = await sync_templates(s, TemplateCatalog.from_dir(directory), SYSTEM_USER_ID)
    finally:
        await engine.dispose()
    for r in results:
        print(f"{r.code:<24} v{r.version:<3} {r.action}{': ' + r.error if r.error else ''}")
    return 1 if any(r.error for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sed_routing.cli")
    p.add_argument("command", choices=["check-templates", "sync-templates"])
    p.add_argument("--dir", type=Path, default=None)
    args = p.parse_args(argv)
    settings = Settings(environment="development")
    directory = args.dir or settings.templates_dir
    try:
        catalog = TemplateCatalog.from_dir(directory)
    except TemplateConfigError as exc:
        print(f"Помилка: {exc.message}", file=sys.stderr)
        return 2
    if args.command == "check-templates":
        for t in catalog:
            chain = " → ".join(s.title_uk for s in t.steps)
            print(f"{t.code} ({t.document_type}): {chain}")
        return 0
    return asyncio.run(_sync(directory, settings.database_url))


if __name__ == "__main__":
    raise SystemExit(main())
