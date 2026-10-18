"""Sparse-представлення (BM25) для гібридного пошуку в Qdrant.

Документна частина BM25 (насичення TF і нормування довжини) рахується тут, IDF — сервером Qdrant
(`Modifier.IDF` на sparse-векторі), тож IDF завжди відповідає поточному корпусу без переіндексації.

Українська морфологія багата, а BM25 рахує лише точні збіги, тому токени проходять легкий
стемінг (відсікання флексій): «ректора», «ректором», «ректору» → «ректор». Для юридичного
тексту цього достатньо: семантичну близькість дає dense-канал (bge-m3), а BM25 потрібен для
точних збігів номерів пунктів, назв посад і термінів («вчена рада», «контракт», «5 років»).
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass

_APOS = str.maketrans({"’": "'", "ʼ": "'", "`": "'", "‘": "'"})
_TOKEN = re.compile(r"[a-zа-яіїєґ']+|\d+(?:[.,]\d+)*", re.IGNORECASE)

# Службові слова, що не несуть змісту для пошуку норм.
STOPWORDS = frozenset("""
а аби або але б без би був була були було бути в вам вас ви від він вона вони воно все всі всіх да
де для до є же з за зі і із її їх й к коли крім ли між мене ми на навіть над нам нас не немає ні
ніж них о об однак от по при про проте с та так також там те тим то тобто того тому тощо у уже хоч
це цей ці цим цих цього чи чий чим що щоб щодо як яка яке які який якщо якої якого якій яких
""".split())

# Флексії, від найдовших до найкоротших. Стем не коротший за 3 літери.
_SUFFIXES = tuple(sorted(set("""
ування ювання ання ення іння ються ється ться ень ити ати ість ості істю остей остям остями
ового ової овому овій овим овими ових ова ове ові ову ою ою ями ами ях ах ям ам ом ем ів їв
ого ому ими ими ій ий ої ою ої іх их им ім ія ії ію ою ей ею
а е и і ї й о у ю я ь
""".split()), key=len, reverse=True))


def stem(token: str) -> str:
    if token.isdigit() or len(token) <= 3:
        return token
    for suf in _SUFFIXES:
        if token.endswith(suf) and len(token) - len(suf) >= 3:
            return token[: -len(suf)]
    return token


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for m in _TOKEN.finditer(text.translate(_APOS).casefold()):
        tok = m.group(0).strip("'")
        if not tok or tok in STOPWORDS or (len(tok) < 2 and not tok.isdigit()):
            continue
        out.append(stem(tok))
    return out


def token_index(token: str) -> int:
    """Стабільний (між процесами й версіями Python) 31-бітний індекс виміру."""
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest(), "big") & 0x7FFFFFFF


@dataclass(frozen=True, slots=True)
class SparseVector:
    indices: list[int]
    values: list[float]


class Bm25Encoder:
    """BM25 без IDF (його застосовує Qdrant). `avg_doc_len` — у токенах; для пунктів положень ~60."""

    def __init__(self, k1: float = 1.2, b: float = 0.75, avg_doc_len: float = 60.0) -> None:
        self.k1 = k1
        self.b = b
        self.avg_doc_len = avg_doc_len

    def encode_document(self, text: str) -> SparseVector:
        tokens = tokenize(text)
        dl = len(tokens) or 1
        tf: dict[int, int] = {}
        for tok, cnt in Counter(tokens).items():
            idx = token_index(tok)
            tf[idx] = tf.get(idx, 0) + cnt     # колізії хешу зливаються (рідкісні при 2^31)
        norm = self.k1 * (1 - self.b + self.b * dl / self.avg_doc_len)
        items = sorted(tf.items())
        return SparseVector([i for i, _ in items], [c * (self.k1 + 1) / (c + norm) for _, c in items])

    def encode_query(self, text: str) -> SparseVector:
        idx = sorted({token_index(t) for t in tokenize(text)})
        return SparseVector(idx, [1.0] * len(idx))

    def config(self) -> dict[str, float]:
        return {"k1": self.k1, "b": self.b, "avg_doc_len": self.avg_doc_len}
