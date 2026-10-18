"""Шаблони маршрутів як конфігурація (TOML) — джерело правди для типових маршрутів.

Структура маршруту (етапи, правила призначення, КЕП, строки) синхронізується в таблиці
`route_templates` / `route_template_steps` (див. `adapters.template_sync`). Поля, яких у схемі
БД поки немає (назва посади для інтерфейсу, напрям проректора, пропуск кроку для автора),
сервіс бере з цього каталогу за кодом шаблону; у README вони винесені в запит на зміну схеми.

Приклад файлу — `templates/memo_procurement.toml`.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sed_db.enums import AssigneeRule, StepKind, UserRole

from .errors import TemplateConfigError


SignatureLevel = Literal["qualified", "internal"]


class StepExtras(BaseModel):
    """Те, що зберігається лише в каталозі (немає колонок у `route_template_steps`)."""

    model_config = ConfigDict(frozen=True)

    title_uk: str
    role_scope: str | None = None   # напрям проректора: збігається з user_roles.scope_note
    skip_if_author: bool = False    # автор сам обіймає цю посаду → крок пропускається
    # Які підписи приймаються: КЕП (qualified) і/або внутрішній ключ АІ-СЕД ПУЕТ (internal).
    signature_levels: tuple[SignatureLevel, ...] = ("qualified",)


class StepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title_uk: str = Field(min_length=1, max_length=256)
    kind: StepKind
    rule: AssigneeRule
    role: UserRole | None = None
    role_scope: str | None = Field(default=None, max_length=256)
    unit: str | None = Field(default=None, description="Код підрозділу (org_units.code)")
    position_code: str | None = Field(default=None, max_length=64)
    user_email: str | None = Field(default=None, description="Для specific_user")
    stage: int | None = Field(default=None, ge=1, description="Однаковий stage — паралельні кроки")
    deadline_hours: int | None = Field(default=None, ge=1, le=24 * 30)
    is_mandatory: bool = True
    requires_kep: bool = True
    skip_if_author: bool = False
    signature_levels: tuple[SignatureLevel, ...] = Field(default=("qualified",), min_length=1)

    @model_validator(mode="after")
    def _params(self) -> StepSpec:
        r = self.rule
        if r is AssigneeRule.SPECIFIC_USER and not self.user_email:
            raise ValueError("specific_user потребує user_email")
        if r is AssigneeRule.POSITION_IN_UNIT and not (self.unit and self.position_code):
            raise ValueError("position_in_unit потребує unit і position_code")
        if r is AssigneeRule.UNIT_HEAD and not self.unit:
            raise ValueError("unit_head потребує unit")
        if r in {AssigneeRule.ROLE, AssigneeRule.AUTHOR_UNIT_ROLE} and self.role is None:
            raise ValueError(f"{r.value} потребує role")
        if self.role_scope and r is not AssigneeRule.ROLE:
            raise ValueError("role_scope має сенс лише для правила role")
        if self.kind is StepKind.SIGNING and not self.requires_kep:
            raise ValueError("Крок підписання завжди з КЕП")
        if self.kind is StepKind.SIGNING and set(self.signature_levels) != {"qualified"}:
            raise ValueError("Підписання (юридична сила документа) — лише кваліфікованим КЕП, без внутрішніх ключів")
        return self

    @property
    def extras(self) -> StepExtras:
        return StepExtras(title_uk=self.title_uk, role_scope=self.role_scope,
                          skip_if_author=self.skip_if_author, signature_levels=self.signature_levels)


class TemplateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(max_length=64, pattern=r"^[A-Z0-9_]+$")
    name_uk: str = Field(min_length=1, max_length=256)
    description: str | None = None
    document_type: str = Field(description="Код виду документа (document_types.code)")
    org_unit: str | None = Field(default=None, description="Код підрозділу; немає — для всього ЗВО")
    steps: tuple[StepSpec, ...] = Field(min_length=1, max_length=50)

    def numbered_steps(self) -> list[tuple[int, int, StepSpec]]:
        """(stage_no, ordinal, крок). Без `stage` кроки йдуть строго послідовно."""
        out: list[tuple[int, int, StepSpec]] = []
        stage = 0
        ordinals: dict[int, int] = {}
        for s in self.steps:
            stage = s.stage if s.stage is not None else stage + 1
            ordinals[stage] = ordinals.get(stage, 0) + 1
            out.append((stage, ordinals[stage], s))
        stages = [st for st, _, _ in out]
        if stages != sorted(stages):
            raise TemplateConfigError(f"Шаблон {self.code}: етапи мають іти за зростанням")
        return out

    def extras(self) -> dict[tuple[int, int], StepExtras]:
        return {(st, o): s.extras for st, o, s in self.numbered_steps()}


class TemplateCatalog:
    """Шаблони з TOML-файлів, проіндексовані за кодом."""

    def __init__(self, templates: Iterable[TemplateSpec] = ()) -> None:
        self._by_code: dict[str, TemplateSpec] = {}
        for t in templates:
            if t.code in self._by_code:
                raise TemplateConfigError(f"Шаблон {t.code} описано двічі")
            t.numbered_steps()  # перевірка порядку етапів
            self._by_code[t.code] = t

    @classmethod
    def from_dir(cls, path: Path) -> TemplateCatalog:
        specs = []
        for f in sorted(path.glob("*.toml")):
            try:
                specs.append(TemplateSpec.model_validate(tomllib.loads(f.read_text(encoding="utf-8"))))
            except (tomllib.TOMLDecodeError, ValueError) as exc:
                raise TemplateConfigError(f"{f.name}: {exc}") from exc
        return cls(specs)

    def __iter__(self):  # noqa: ANN204
        return iter(self._by_code.values())

    def get(self, code: str) -> TemplateSpec | None:
        return self._by_code.get(code)

    def step_extras(self, code: str | None, stage_no: int, ordinal: int) -> StepExtras | None:
        spec = self._by_code.get(code) if code else None
        return spec.extras().get((stage_no, ordinal)) if spec else None
