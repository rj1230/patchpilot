"""Hybrid retrieval: BM25 + hashed-vector similarity, fused with RRF, then test/import graph expansion."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from .indexer import Chunk, RepoIndex

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
STOP = {"the", "a", "an", "is", "to", "of", "and", "in", "on", "for", "it", "this", "that", "with", "as", "be", "not", "when"}


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for w in _TOKEN.findall(text):
        parts = [p.lower() for p in _CAMEL.split(w)]
        full = w.lower()
        out.append(full)
        if len(parts) > 1:
            out += parts
    return [t for t in out if t not in STOP and len(t) > 1]


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [Counter(d) for d in docs]
        self.len = [len(d) for d in docs]
        self.avg = (sum(self.len) / len(self.len)) if docs else 0.0
        df: Counter = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def score(self, query: list[str]) -> list[float]:
        out = []
        for tf, ln in zip(self.docs, self.len):
            s = 0.0
            for t in set(query):
                if t in tf:
                    f = tf[t]
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / (self.avg or 1)))
            out.append(s)
        return out


def hash_embed(tokens: list[str], dim: int = 256) -> list[float]:
    """Deterministic feature-hashing embedding. Swap for a real embedding model/pgvector in production."""
    import hashlib

    v = [0.0] * dim
    grams = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
    for g in grams:
        h = int(hashlib.md5(g.encode()).hexdigest(), 16)
        v[h % dim] += 1.0 if (h >> 64) & 1 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


@dataclass
class Hit:
    chunk: Chunk
    score: float
    why: str  # hybrid | test_link | import_link


class Retriever:
    def __init__(self, index: RepoIndex):
        self.index = index
        self.chunks = [c for c in index.chunks]
        # symbol name weighted by repetition so it dominates matching
        self.toks = [tokenize(f"{c.symbol} {c.symbol} {c.path} {c.text}") for c in self.chunks]
        self.bm25 = BM25(self.toks)
        self.vecs = [hash_embed(t) for t in self.toks]

    def search(self, query: str, k: int = 10, expand: bool = True) -> list[Hit]:
        qt = tokenize(query)
        if not qt or not self.chunks:
            return []
        lex = self.bm25.score(qt)
        qv = hash_embed(qt)
        vec = [cosine(qv, v) for v in self.vecs]
        rrf: dict[int, float] = {}
        for ranking in (lex, vec):
            order = sorted(range(len(ranking)), key=lambda i: -ranking[i])
            for rank, i in enumerate(order[:50]):
                if ranking[i] > 0:
                    rrf[i] = rrf.get(i, 0.0) + 1.0 / (60 + rank)
        # module-level header chunks are rarely the answer; mild demotion keeps symbols on top
        for i in list(rrf):
            if self.chunks[i].kind == "module":
                rrf[i] *= 0.6
        top = sorted(rrf, key=lambda i: -rrf[i])[:k]
        hits = [Hit(self.chunks[i], rrf[i], "hybrid") for i in top]
        if expand:
            hits = self._expand(hits, k)
        return hits[:k]

    def _expand(self, hits: list[Hit], k: int) -> list[Hit]:
        seen = {(h.chunk.path, h.chunk.symbol) for h in hits}
        extra: list[Hit] = []
        for h in hits[:3]:
            c = h.chunk
            stem = c.path.rsplit("/", 1)[-1].removesuffix(".py")
            if not c.is_test:  # source -> its tests
                for t in self.chunks:
                    if t.is_test and t.kind != "module" and (f"test_{stem}" in t.path or f"{stem}_test" in t.path):
                        key = (t.path, t.symbol)
                        if key not in seen:
                            seen.add(key)
                            extra.append(Hit(t, h.score * 0.5, "test_link"))
        # keep primary hybrid hits first, linked evidence after, within k
        room = max(0, k - len(hits))
        if room == 0 and extra:  # always leave space for at least the linked tests
            hits = hits[: k - min(len(extra), 3)]
            room = k - len(hits)
        return hits + extra[:room]
