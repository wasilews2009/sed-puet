"""Розбір проєкту розпорядчого документа на пункти для перевірки.

Наказ за ДСТУ 4163:2020: заголовок «Про …», констатувальна частина («Відповідно до …, з метою …»),
слово «НАКАЗУЮ:», пункти розпорядчої частини, «Підстава: …», підпис (посада + ім'я). Кожен пункт
розпорядчої частини перевіряється окремо: для нього шукаються свої норми, і зауваження
прив'язується до конкретного пункту проєкту. Посада підписанта передається як контекст —
від неї залежить, чи не перевищено повноваження.

Розбір працює на тексті з мітками ПД: структура (нумерація, «НАКАЗУЮ») від маскування не змінюється.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..corpus.segmentation import split_long
from ..domain.models import DraftContext, Provision, ProvisionKind

_OPERATIVE = re.compile(
    r"(?m)(?:^|\s)(НАКАЗУЮ|ЗОБОВ['’ʼ]ЯЗУЮ|ПОСТАНОВЛЯЮ|ПРОПОНУЮ|ВИРІШИЛА|ВИРІШИВ|УХВАЛИЛИ|ВИРІШИЛИ)\s*:")
_KIND = re.compile(r"(?m)^\s*(НАКАЗ|РОЗПОРЯДЖЕННЯ|ПОСТАНОВА|РІШЕННЯ|ПОЛОЖЕННЯ|ІНСТРУКЦІЯ)\b")
_TITLE = re.compile(r"(?m)^\s*((?:Про|Щодо)\s+\S.*)$")
_ITEM = re.compile(r"^\s*(\d{1,2})((?:\.\d{1,2}){0,3})\.?\)?\s+(\S.*)$")
_BASIS = re.compile(r"^\s*Підстав[аи]\s*:", re.IGNORECASE)
_TAIL_MARK = re.compile(
    r"^\s*(?:ПОГОДЖЕНО|Погоджено|Візи|ВІЗИ|Проєкт вносить|Проект вносить|З наказом ознайомлен|Розіслати|"
    r"Виконавець|Вик\.)", re.IGNORECASE)
_POSITION = (r"(?:В\.?\s?о\.\s*|Т\.?\s?в\.?\s?о\.\s*)?(?:Перш(?:ий|ого)\s+)?"
             r"(?:[Рр]ектор\w*|[Пп]роректор\w*(?:\s+з\s+[^\[\n]{3,80}?)?|[Дд]иректор\w*(?:\s+[^\[\n]{3,80}?)?|"
             r"[Дд]екан\w*(?:\s+[^\[\n]{3,80}?)?|[Зз]авідувач\w*(?:\s+[^\[\n]{3,80}?)?|"
             r"[Гг]олов[аи](?:\s+[^\[\n]{3,80}?)?|[Нн]ачальник\w*(?:\s+[^\[\n]{3,80}?)?)")
_SIGNATURE = re.compile(
    rf"^\s*(?P<pos>{_POSITION})\s*(?:\[ОСОБА_\d+\]|[А-ЯІЇЄҐ][а-яіїєґ'’ʼ]+\s+[А-ЯІЇЄҐ'’ʼ]{{2,}}|_+.*)?\s*$")

KIND_LABELS = {"НАКАЗ": "наказ", "РОЗПОРЯДЖЕННЯ": "розпорядження", "ПОСТАНОВА": "постанова",
               "РІШЕННЯ": "рішення", "ПОЛОЖЕННЯ": "положення", "ІНСТРУКЦІЯ": "інструкція"}


@dataclass(frozen=True, slots=True)
class ParsedDraft:
    context: DraftContext
    provisions: list[Provision]
    structured: bool             # знайдено розпорядчу частину; інакше — поділ на абзаци


def parse_draft(masked: str, *, max_provisions: int = 40, max_chars: int = 1800) -> ParsedDraft:
    text = masked.replace("\r\n", "\n").replace(" ", " ")
    lines = text.split("\n")

    kind_m = _KIND.search(text)
    kind_label = KIND_LABELS.get(kind_m.group(1), "документ") if kind_m else "документ"
    signer, sig_line = _find_signer(lines)

    op = _OPERATIVE.search(text)
    if op is None:
        return _unstructured(text, lines, kind_label, signer, sig_line, max_provisions, max_chars)

    head = text[: op.start(1)]
    title_m = _TITLE.search(head)
    title = _title_block(head, title_m) if title_m else None
    pre_start = (title_m.end() if title_m else 0)
    preamble = head[pre_start:].strip()
    if title and preamble.startswith(title):
        preamble = preamble[len(title):].strip()
    preamble = re.sub(r"\s+", " ", preamble).strip() or None

    sig_pos = sum(len(ln) + 1 for ln in lines[:sig_line]) if sig_line is not None else len(text)
    body_lines = text[op.end():max(sig_pos, op.end())].split("\n")
    items: list[tuple[str, list[str]]] = []
    basis: list[str] = []
    in_basis = False
    for ln in body_lines:
        s = ln.strip()
        if not s:
            continue
        if _TAIL_MARK.match(s):
            break
        if _BASIS.match(s):
            in_basis = True
            basis.append(s)
            continue
        if in_basis:
            basis.append(s)
            continue
        m = _ITEM.match(s)
        if m:
            num = m.group(1) + m.group(2)
            if not m.group(2) or not items:
                items.append((num, [s]))
            else:
                items[-1][1].append(s)
            continue
        if items:
            items[-1][1].append(s)
        else:
            items.append(("", [s]))

    provisions: list[Provision] = []
    if preamble:
        provisions.append(Provision("P0", ProvisionKind.PREAMBLE, "констатувальна частина", preamble))
    for num, chunk in items:
        body = " ".join(chunk)
        label = f"п. {num} розпорядчої частини" if num else "розпорядча частина"
        for j, part in enumerate(_split(chunk, body, max_chars)):
            suffix = f" (частина {j + 1})" if j else ""
            provisions.append(Provision(f"P{len(provisions) + 1}", ProvisionKind.ITEM, label + suffix, part))
    if basis:
        provisions.append(Provision(f"P{len(provisions) + 1}", ProvisionKind.BASIS, "підстава", " ".join(basis)))

    ctx = DraftContext(title=title, kind_label=kind_label, signer_position=signer, preamble=preamble)
    return ParsedDraft(ctx, _renumber(provisions[:max_provisions]), structured=True)


def _title_block(head: str, m: re.Match[str]) -> str:
    """Заголовок може займати кілька рядків — до порожнього рядка або «Відповідно…»."""
    parts = [m.group(1).strip()]
    for ln in head[m.end():].split("\n")[1:]:
        s = ln.strip()
        if not s or re.match(r"^(Відповідно|З метою|На виконання|Керуючись|На підставі|У зв['’ʼ]язку)", s):
            break
        parts.append(s)
    return " ".join(parts)


def _find_signer(lines: list[str]) -> tuple[str | None, int | None]:
    for i in range(len(lines) - 1, max(-1, len(lines) - 25), -1):
        s = lines[i].strip()
        if not s:
            continue
        m = _SIGNATURE.match(s)
        if m and not _ITEM.match(s):
            return re.sub(r"\s+", " ", m.group("pos")).strip(), i
    return None, None


def _split(chunk: list[str], body: str, max_chars: int) -> list[str]:
    """Довгий пункт ділиться за підпунктами (рядками), а надто довгий рядок — за реченнями."""
    if len(body) <= max_chars:
        return [body]
    parts: list[str] = []
    buf = ""
    for unit in (u for ln in chunk for u in split_long(ln, max_chars)):
        if buf and len(buf) + len(unit) + 1 > max_chars:
            parts.append(buf)
            buf = unit
        else:
            buf = f"{buf} {unit}".strip()
    if buf:
        parts.append(buf)
    return parts


def _unstructured(text: str, lines: list[str], kind_label: str, signer: str | None, sig_line: int | None,
                  max_provisions: int, max_chars: int) -> ParsedDraft:
    """Документ без розпорядчої частини (службова записка, проєкт положення): абзаци до `max_chars`."""
    title_m = _TITLE.search(text)
    title = title_m.group(1).strip() if title_m else None
    body = "\n".join(lines[:sig_line] if sig_line is not None else lines)
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n|\n(?=\d{1,2}\.\s)", body)]
    paras = [p for p in paras if len(p) >= 20 and p != title]
    provisions: list[Provision] = []
    buf = ""
    for p in paras:
        if buf and len(buf) + len(p) + 1 > max_chars:
            provisions.append(Provision("", ProvisionKind.ITEM, "", buf))
            buf = p
        else:
            buf = f"{buf} {p}".strip()
    if buf:
        provisions.append(Provision("", ProvisionKind.ITEM, "", buf))
    provisions = [Provision(f"P{i + 1}", p.kind, f"фрагмент {i + 1}", p.text)
                  for i, p in enumerate(provisions[:max_provisions])]
    ctx = DraftContext(title=title, kind_label=kind_label, signer_position=signer, preamble=None)
    return ParsedDraft(ctx, provisions, structured=False)


def _renumber(provisions: list[Provision]) -> list[Provision]:
    return [Provision(f"P{i + 1}", p.kind, p.label, p.text) for i, p in enumerate(provisions)]
