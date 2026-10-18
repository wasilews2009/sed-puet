"""Сегментація нормативного акта на пункти для індексації.

Одиниця пошуку й цитування — **пункт** акта («п. 7.1.3», «ст. 34, ч. 1»), а не вікно в N токенів:
саме пункт людина відкриває в першоджерелі, і саме на нього посилається зауваження. Довгий пункт
ділиться на частини за підпунктами («1)», «а)») або реченнями з тим самим посиланням і позначкою
частини; заголовки розділів зберігаються як контекст (`heading_path`), що додається до тексту
для ембедингу, але не до цитати.

Підтримуються типові для ЗВО і МОН формати:
* «РОЗДІЛ 7. ОРГАНИ УПРАВЛІННЯ», «Розділ IV», «IV. ПРАВА ТА ОБОВ'ЯЗКИ», «7. ОРГАНИ УПРАВЛІННЯ» (капсом);
* «7.1.3.», «7.1.3», «1.» — пункти з багаторівневою нумерацією;
* «Стаття 34. Ректор закладу вищої освіти» + «1. …» — частини статей закону.

Текст після OCR очищується: переноси «сло-\\nво», колонтитули, номери сторінок, зайві розриви рядків.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from ..domain.models import Clause

_PAGE_MARK = re.compile(r"^\s*\[\[page \d+\]\]\s*$")
_PAGE_NUM = re.compile(r"^\s*(?:[-–—]\s*)?\d{1,3}(?:\s*[-–—])?\s*$|^\s*(?:сторінка|стор\.|с\.)\s*\d+(?:\s*(?:з|із)\s*\d+)?\s*$",
                       re.IGNORECASE)
_HYPHEN_BREAK = re.compile(r"(\w)[-‐‑]\n(\w)")
_ROMAN = r"(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"
_ROMAN_UA = str.maketrans({"І": "I", "Х": "X", "У": "V"})  # OCR часто дає кирилицю замість латиниці

_RE_SECTION_WORD = re.compile(rf"^\s*(?:РОЗДІЛ|Розділ|ГЛАВА|Глава)\s+(\d+|{_ROMAN}|[IІVХX]+)\b\.?\s*(.*)$")
_RE_SECTION_ROMAN = re.compile(r"^\s*([IІVХX]{1,6})\.\s+(\S.*)$")
_RE_ARTICLE = re.compile(r"^\s*Стаття\s+(\d+(?:[-‐]\d+)?)\.?\s*(.*)$")
# «7.2.13.», «1.1,» (OCR: кома замість крапки), «7.2.11.В Університеті» (OCR: без пробілу), «. 7.2.14.»
_RE_NUMBERED = re.compile(r"^[\s.,|'\"_-]*(\d{1,2}(?:\.\d{1,3}){0,4})([.,)])?\s*([^\W\d_«\"(].*|[«\"(].*)$")
_UPPER_RATIO = 0.7


@dataclass(slots=True)
class _Open:
    number: str | None
    heading_path: tuple[str, ...]
    article: str | None
    section: str | None
    lines: list[str] = field(default_factory=list)


def clean_text(raw: str) -> str:
    """Нормалізація тексту акта (зокрема після OCR) зі збереженням розбиття на рядки."""
    text = unicodedata.normalize("NFC", raw).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("­", "").replace(" ", " ").replace("\f", "\n")
    text = re.sub(r"(?<=\s)[|¦](?=\s)|^[|¦]\s+|\s+[|¦]$", " ", text, flags=re.MULTILINE)  # OCR: лінії таблиць
    text = re.sub(r"[ \t]+", " ", text)
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if not _PAGE_MARK.match(ln) and not _PAGE_NUM.match(ln)]
    # Колонтитули: короткий рядок, що повторюється на багатьох сторінках (індекс документа, назва ЗВО).
    pages = max(1, raw.count("[[page ") or raw.count("\f") + 1)
    if pages >= 3:
        counts = Counter(ln for ln in lines if 0 < len(ln) <= 90)
        running = {ln for ln, c in counts.items() if c >= max(3, int(pages * 0.4))}
        lines = [ln for ln in lines if ln not in running]
    return "\n".join(lines)


def _is_upper_heading(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    return len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) >= _UPPER_RATIO


def _roman(s: str) -> str:
    return s.translate(_ROMAN_UA)


def segment(text: str, *, max_chars: int = 1500) -> list[Clause]:
    """Розбити очищений текст акта на пункти. Порядок збігається з порядком у тексті."""
    clauses: list[Clause] = []
    headings: list[str] = []          # шлях заголовків розділів
    section: str | None = None        # «розд. 7» / «розд. IV»
    article: str | None = None        # «ст. 34»
    cur: _Open | None = None
    preamble: list[str] = []
    last_number: str | None = None

    def flush() -> None:
        nonlocal cur
        if cur is None:
            return
        body = _join_lines(cur.lines)
        if body:
            ref = _ref(cur.section, cur.article, cur.number)
            for i, part in enumerate(split_long(body, max_chars)):
                clauses.append(Clause(
                    section_ref=ref if i == 0 else f"{ref} (продовження {i + 1})",
                    heading_path=cur.heading_path, text=part, number=cur.number,
                ))
        cur = None

    for line in text.split("\n"):
        if not line:
            if cur is not None:
                cur.lines.append("")
            continue

        m = _RE_SECTION_WORD.match(line)
        if m:
            flush()
            num = _roman(m.group(1))
            section, article = f"розд. {num}", None
            headings = [f"Розділ {num}" + (f". {m.group(2).strip()}" if m.group(2).strip() else "")]
            continue
        m = _RE_SECTION_ROMAN.match(line)
        if m and _is_upper_heading(m.group(2)):
            flush()
            num = _roman(m.group(1))
            section, article = f"розд. {num}", None
            headings = [f"{num}. {m.group(2).strip()}"]
            continue
        m = _RE_ARTICLE.match(line)
        if m:
            flush()
            article = f"ст. {m.group(1)}"
            title = m.group(2).strip()
            headings = [*headings[:1], f"Стаття {m.group(1)}" + (f". {title}" if title else "")]
            cur = _Open(None, tuple(headings), article, section)
            continue
        m = _RE_NUMBERED.match(line)
        if m and "." not in m.group(1) and not (m.group(2) and (m.group(3)[0].isupper() or m.group(3)[0] in "«\"(")):
            m = None                      # «5 років …» — не пункт: однорівневий номер потребує «.»/«)» і великої літери
        if m:
            number, rest = _repair_number(m.group(1), last_number), m.group(3)
            last_number = number
            depth = number.count(".") + 1
            if depth == 1 and _is_upper_heading(rest) and len(rest) <= 160 and article is None:
                # «7. ОРГАНИ УПРАВЛІННЯ УНІВЕРСИТЕТОМ» — заголовок розділу з арабським номером.
                flush()
                section = f"розд. {number}"
                headings = [f"{number}. {rest.strip()}"]
                continue
            if m.group(2) == ")" and cur is not None:
                # «1) …» — підпункт усередині пункту, не окрема структурна одиниця.
                cur.lines.append(line)
                continue
            flush()
            cur = _Open(number, tuple(headings), article, section, [rest])
            continue

        if cur is None:
            if headings and _is_upper_heading(line) and len(line) <= 160 and len(headings) == 1:
                # Багаторядковий заголовок розділу.
                headings[0] = f"{headings[0]} {line}".strip()
                continue
            if not headings and not clauses:
                preamble.append(line)
                continue
            cur = _Open(None, tuple(headings), article, section)
        cur.lines.append(line)
    flush()

    clauses = _promote_subheadings(clauses)
    if preamble and not clauses:
        body = _join_lines(preamble)
        return [Clause(section_ref="текст акта", heading_path=(), text=p) for p in split_long(body, max_chars)]
    return clauses


def _parts(number: str) -> list[int]:
    return [int(x) for x in number.split(".") if x]


def _plausible_next(prev: str, cand: str) -> bool:
    """«7.2.14» після «7.2.13», «7.3» після «7.2.16», «8» після «7.4.2», «7.2.1» після «7.2»."""
    p, c = _parts(prev), _parts(cand)
    if c[: len(p)] == p and len(c) == len(p) + 1 and c[-1] == 1:
        return True
    return any(c[:k] == p[:k] and len(c) > k and c[k] == p[k] + 1 for k in range(len(p)))


def _repair_number(number: str, prev: str | None) -> str:
    """OCR губить крапку в номері: «72.14» замість «7.2.14». Виправляємо, лише якщо оригінальний
    номер не продовжує нумерацію, а виправлений — продовжує."""
    if prev is None or _plausible_next(prev, number):
        return number
    head, _, tail = number.partition(".")
    if len(head) == 2:
        fixed = f"{head[0]}.{head[1]}" + (f".{tail}" if tail else "")
        if _plausible_next(prev, fixed):
            return fixed
    return number


def _ref(section: str | None, article: str | None, number: str | None) -> str:
    if article:
        return f"{article}, ч. {number}" if number else article
    if number:
        return f"п. {number}" if "." in number or not section else f"{section}, п. {number}"
    return section or "вступна частина"


def _promote_subheadings(clauses: list[Clause]) -> list[Clause]:
    """«7.1. Ректор» без розділового знака, за яким ідуть «7.1.1 …», — це підзаголовок:
    він стає частиною `heading_path` дочірніх пунктів, а не окремим фрагментом для пошуку."""
    out: list[Clause] = []
    sub: tuple[str, str] | None = None   # (номер, заголовок)
    for i, c in enumerate(clauses):
        nxt = clauses[i + 1] if i + 1 < len(clauses) else None
        if (c.number and (len(c.text) <= 90 or (_is_upper_heading(c.text) and len(c.text) <= 200))
                and not c.text.rstrip().endswith((".", ";", ":"))
                and nxt is not None and nxt.number and nxt.number.startswith(c.number + ".")):
            sub = (c.number, f"{c.number}. {c.text.strip()}")
            continue
        if sub and c.number and c.number.startswith(sub[0] + "."):
            c = Clause(c.section_ref, (*c.heading_path, sub[1]), c.text, c.number)
        elif sub and (not c.number or not c.number.startswith(sub[0] + ".")):
            sub = None
        out.append(c)
    return out


def _join_lines(lines: list[str]) -> str:
    """Склеює рядки, розірвані версткою; зберігає абзаци й підпункти «1)», «а)», «—»."""
    paras: list[str] = []
    buf: list[str] = []
    for ln in lines:
        if not ln:
            if buf:
                paras.append(" ".join(buf))
                buf = []
            continue
        if buf and re.match(r"^(?:\d{1,2}\)|[а-яіїє]\)|[-–—•]\s)", ln):
            paras.append(" ".join(buf))
            buf = []
        buf.append(ln)
    if buf:
        paras.append(" ".join(buf))
    return "\n".join(p.strip() for p in paras if p.strip())


_SENT_SPLIT = re.compile(r"(?<=[.;:])\s+(?=[А-ЯІЇЄҐ\d«(—–-])")


def split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    units: list[str] = []
    for para in text.split("\n"):
        units.extend(_SENT_SPLIT.split(para) if len(para) > max_chars else [para])
    parts: list[str] = []
    buf = ""
    for u in units:
        if buf and len(buf) + len(u) + 1 > max_chars:
            parts.append(buf)
            buf = u
        else:
            buf = f"{buf}\n{u}" if buf else u
        while len(buf) > max_chars:                       # одне речення довше за ліміт
            cut = buf.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            parts.append(buf[:cut])
            buf = buf[cut:].lstrip()
    if buf:
        parts.append(buf)
    return parts
