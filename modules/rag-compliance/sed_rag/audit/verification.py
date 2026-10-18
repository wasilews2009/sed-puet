"""Звірка цитат: захист від галюцинованих норм і вигаданих фрагментів проєкту.

`compliance_citations.quote_verified = true` означає одне: `quoted_text` буквально міститься у
фрагменті корпусу. Тому в БД завжди йде фрагмент саме з тексту норми — дослівний збіг з цитатою
моделі або (якщо модель перефразувала) найближче речення норми, — а розбіжність з тим, що
навела модель, знижує confidence зауваження і пояснюється в `issues`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from ..corpus.sparse import tokenize

_TRANS = str.maketrans({
    "’": "'", "ʼ": "'", "`": "'", "‘": "'", "«": '"', "»": '"', "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "‐": "-", "‑": "-", " ": " ",
})
_SENT = re.compile(r"[^.;:!?\n]+(?:[.;:!?]+|$)")


class QuoteMatch(StrEnum):
    EXACT = "exact"            # дослівно (з точністю до пробілів, лапок, тире, регістру)
    FUZZY = "fuzzy"            # перефразовано незначно: ≥ 80 % слів цитати в одному реченні
    NEAREST = "nearest"        # підставлено найближче речення норми (40–80 % слів)
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Located:
    match: QuoteMatch
    text: str                  # дослівний фрагмент з тексту-джерела
    start: int | None
    end: int | None
    overlap: float


def _normalize_with_map(s: str) -> tuple[str, list[int]]:
    """Нормалізований рядок і позиція кожного його символу в оригіналі."""
    out: list[str] = []
    idx: list[int] = []
    prev_space = True
    for i, ch in enumerate(s.translate(_TRANS)):
        if ch.isspace():
            if not prev_space:
                out.append(" ")
                idx.append(i)
            prev_space = True
            continue
        out.append(ch.casefold())
        idx.append(i)
        prev_space = False
    return "".join(out), idx


def _norm(s: str) -> str:
    return _normalize_with_map(s)[0].strip(" .,;:\"'")


def locate(quote: str, source: str) -> Located:
    q = _norm(quote).replace("…", "").replace("...", "")
    if len(q) < 8:
        return Located(QuoteMatch.NONE, "", None, None, 0.0)
    ns, idx = _normalize_with_map(source)
    pos = ns.find(q)
    if pos >= 0:
        start, end = idx[pos], idx[pos + len(q) - 1] + 1
        return Located(QuoteMatch.EXACT, source[start:end], start, end, 1.0)

    # Цитата могла бути скорочена «…»: перевіряємо кожну частину.
    parts = [p.strip() for p in re.split(r"\s*(?:…|\.\.\.)\s*", quote) if len(p.strip()) >= 12]
    if len(parts) > 1:
        located = [locate(p, source) for p in parts]
        if all(lc.match is QuoteMatch.EXACT for lc in located):
            start = min(lc.start for lc in located if lc.start is not None)
            end = max(lc.end for lc in located if lc.end is not None)
            return Located(QuoteMatch.EXACT, source[start:end], start, end, 1.0)

    q_tokens = set(tokenize(quote))
    if not q_tokens:
        return Located(QuoteMatch.NONE, "", None, None, 0.0)
    best: tuple[float, int, int] = (0.0, 0, 0)
    sentences = [(m.start(), m.end()) for m in _SENT.finditer(source) if m.group(0).strip()]
    # Вікна з 1–2 речень: цитата може захоплювати межу речень.
    windows = sentences + [(a[0], b[1]) for a, b in zip(sentences, sentences[1:])]
    for s, e in windows:
        t = set(tokenize(source[s:e]))
        if not t:
            continue
        overlap = len(q_tokens & t) / len(q_tokens)
        # Серед рівних — коротше вікно (точніша цитата).
        if overlap > best[0] + 1e-9 or (abs(overlap - best[0]) < 1e-9 and best[2] - best[1] > e - s):
            best = (overlap, s, e)
    overlap, s, e = best
    while s < e and source[s].isspace():
        s += 1
    text = source[s:e].strip()
    if overlap >= 0.8:
        return Located(QuoteMatch.FUZZY, text, s, s + len(text), round(overlap, 3))
    if overlap >= 0.4:
        return Located(QuoteMatch.NEAREST, text, s, s + len(text), round(overlap, 3))
    return Located(QuoteMatch.NONE, text, s, s + len(text), round(overlap, 3))


def first_sentences(text: str, max_chars: int = 400) -> str:
    out = ""
    for m in _SENT.finditer(text):
        piece = m.group(0).strip()
        if not piece:
            continue
        if out and len(out) + len(piece) + 1 > max_chars:
            break
        out = f"{out} {piece}".strip()
    return out or text[:max_chars]
