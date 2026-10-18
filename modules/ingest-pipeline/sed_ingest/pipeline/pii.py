"""Псевдонімізація персональних даних перед передачею тексту в LLM.

ПД замінюються мітками `[ОСОБА_1]`, `[РНОКПП_1]` … Модель бачить і повертає лише мітки,
а справжні значення підставляються назад локально (`restore`) і нікуди не логуються:
словник «мітка → оригінал» живе тільки в пам'яті запиту.

Розпізнавачі — правила для українських форматів (РНОКПП, IBAN UA, телефони +380,
паспорт / ID-картка, ПІБ із по батькові, ініціали, підпис «Ім'я ПРІЗВИЩЕ» за ДСТУ 4163).
Порт `PiiMasker` дозволяє замінити цей клас на Presidio + spaCy `uk_core_news_lg`
(ці самі правила тоді стають custom recognizers), не змінюючи пайплайн.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from ..domain.models import MaskingResult

_UP = "А-ЯІЇЄҐ"
_LO = "а-яіїєґ"
_APOS = "'’ʼ`"
_NAME = rf"[{_UP}][{_LO}{_APOS}]+(?:-[{_UP}][{_LO}{_APOS}]+)?"
# По батькові в усіх відмінках: Петрович/Петровича/Петровичу/Петровичем, Іванівна/Іванівни/Іванівною…
_PATRONYMIC = rf"[{_UP}][{_LO}{_APOS}]+(?:ович|евич|євич|іч)(?:а|у|ем|еві)?|[{_UP}][{_LO}{_APOS}]+(?:івн|ївн|овн)(?:а|и|і|у|ою)"
_INITIAL = rf"[{_UP}]\.[ \t]?"

# Абревіатури великими літерами, які не є прізвищами в підписі «Ім'я ПРІЗВИЩЕ».
_CAPS_STOP = frozenset({
    "ПУЕТ", "МОН", "КМУ", "ВРУ", "ДСТУ", "ЗВО", "ЄДЕБО", "НАКАЗУЮ", "ЗОБОВ'ЯЗУЮ", "ПОГОДЖЕНО",
    "ЗАТВЕРДЖУЮ", "ЗАТВЕРДЖЕНО", "НАКАЗ", "РОЗПОРЯДЖЕННЯ", "ЗАЯВА", "ПРОТОКОЛ", "ПОЛОЖЕННЯ",
    "ННІ", "ННЦ", "НМР", "КЕП", "ПДВ", "ФОП", "ТОВ", "ПРАТ", "ДП", "ЗУ", "ЄС", "ГРН", "СЛУХАЛИ",
    "ВИСТУПИЛИ", "УХВАЛИЛИ", "ВИРІШИЛИ", "ПРОПОНУЮ", "ПРОШУ", "ДОРУЧАЮ", "ВІЗИ",
})


@dataclass(frozen=True, slots=True)
class _Recognizer:
    label: str
    pattern: re.Pattern[str]
    group: str | None = None   # якщо задано — маскується лише ця група (контекст лишається)


_RECOGNIZERS: tuple[_Recognizer, ...] = (
    _Recognizer("ЕЛ_ПОШТА", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    _Recognizer("РАХУНОК", re.compile(r"\bUA\d{2}(?:\s?\d){25}\b")),
    _Recognizer("ТЕЛЕФОН", re.compile(
        r"(?<![\d.])(?:\+?38\s?)?\(?0\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?![\d.])")),
    _Recognizer("РНОКПП", re.compile(r"(?<![\d/.-])\d{10}(?![\d/.-])")),
    _Recognizer("ПАСПОРТ", re.compile(rf"\b[{_UP}]{{2}}\s?№?\s?\d{{6}}\b")),
    _Recognizer("ПАСПОРТ", re.compile(
        r"(?i:паспорт\w*|id-карт\w*|ід-карт\w*)[^\d\n]{0,30}(?P<doc>(?<!\d)\d{9}(?!\d))"), group="doc"),
    # Прізвище Ім'я По батькові  /  Ім'я По батькові Прізвище
    _Recognizer("ОСОБА", re.compile(rf"\b{_NAME}\s+{_NAME}\s+(?:{_PATRONYMIC})\b")),
    _Recognizer("ОСОБА", re.compile(rf"\b{_NAME}\s+(?:{_PATRONYMIC})\s+{_NAME}\b")),
    # О. П. Петренко / О.П. Петренко / Петренко О. П. / Петренко О.
    _Recognizer("ОСОБА", re.compile(rf"(?<![{_UP}{_LO}]){_INITIAL}(?:{_INITIAL})?{_NAME}\b")),
    _Recognizer("ОСОБА", re.compile(rf"\b{_NAME}[ \t]{_INITIAL}(?:{_INITIAL})?(?![{_LO}])")),
    # Підпис за ДСТУ 4163: «Олена ПЕТРЕНКО»
    _Recognizer("ОСОБА", re.compile(rf"\b{_NAME}[ \t]+(?P<caps>[{_UP}{_APOS}]{{3,}}(?:-[{_UP}]{{2,}})?)\b")),
)

_PLACEHOLDER = re.compile(r"\[(?:ОСОБА|РНОКПП|ТЕЛЕФОН|ЕЛ_ПОШТА|РАХУНОК|ПАСПОРТ)_\d+\]")


class RegexPseudonymizer:
    """Реалізація порту `PiiMasker`."""

    def mask(self, text: str) -> MaskingResult:
        spans: list[tuple[int, int, str]] = []
        for rec in _RECOGNIZERS:
            for m in rec.pattern.finditer(text):
                if rec.label == "ОСОБА" and "caps" in m.groupdict() and m.group("caps") in _CAPS_STOP:
                    continue
                start, end = m.span(rec.group) if rec.group else m.span()
                spans.append((start, end, rec.label))
        # Неперекривні фрагменти: раніший початок, довший фрагмент виграє.
        spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
        chosen: list[tuple[int, int, str]] = []
        last_end = -1
        for s in spans:
            if s[0] >= last_end:
                chosen.append(s)
                last_end = s[1]

        mapping: dict[str, str] = {}
        reverse: dict[tuple[str, str], str] = {}
        counts: Counter[str] = Counter()
        out: list[str] = []
        pos = 0
        for start, end, label in chosen:
            original = text[start:end]
            key = (label, _canon(original))
            token = reverse.get(key)
            if token is None:
                counts[label] += 1
                token = f"[{label}_{counts[label]}]"
                reverse[key] = token
                mapping[token] = original
            out.append(text[pos:start])
            out.append(token)
            pos = end
        out.append(text[pos:])
        return MaskingResult(masked_text="".join(out), mapping=mapping, counts=dict(counts))

    def restore(self, value: str, mapping: dict[str, str]) -> str:
        return _PLACEHOLDER.sub(lambda m: mapping.get(m.group(0), m.group(0)), value)


def _canon(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def contains_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER.search(value))
