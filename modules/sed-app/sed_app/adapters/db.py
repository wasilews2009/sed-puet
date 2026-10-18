"""Спільне для SQL-адаптерів: актор транзакції для тригерів аудиту і бізнес-події журналу."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def set_actor(s: AsyncSession, user_id: uuid.UUID, request_id: str | None, reason: str | None = None) -> None:
    """Тригери `audit_logs` і `*_history` беруть актора з цих змінних (лише до кінця транзакції)."""
    await s.execute(
        text("SELECT set_config('sed.actor_user_id', :u, true), set_config('sed.actor_type', 'user', true),"
             "       set_config('sed.request_id', :r, true), set_config('sed.change_reason', :c, true)"),
        {"u": str(user_id), "r": request_id or "", "c": reason or ""},
    )


async def audit_event(s: AsyncSession, action: str, entity_table: str, entity_id: uuid.UUID,
                      document_id: uuid.UUID | None, details: dict[str, Any] | None = None) -> None:
    """Бізнес-подія (перегляд картки, завантаження файлу) у журнал з хеш-ланцюжком."""
    await s.execute(
        text("SELECT sed_audit_event(:a, :t, :e, :d, CAST(:j AS jsonb))"),
        {"a": action, "t": entity_table, "e": entity_id, "d": document_id,
         "j": json.dumps(details or {}, ensure_ascii=False)},
    )
