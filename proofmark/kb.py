"""Find existing Help Centre articles that a new feature probably makes stale.

Lexical TF-IDF with cosine similarity: no model, no index, instant, and
explainable (we show the shared terms and the sentence that matched). Drop
exported articles as Markdown files into knowledge_base/ to use your own.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .schemas import FactSheet, KBMatch

STOPWORDS = set("""
a about after again all also am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers how i
if in into is it its itself just me more most my no nor not now of off on once only or other our ours out over
own same she should so some such than that the their theirs them then there these they this those through to
too under until up very was we were what when where which while who whom why will with would you your yours
yourself get got use using used new now also may might must can't don't won't one two three
per take set way right away like every stay still make made
""".split())


def tokenize(text: str) -> list[str]:
    tokens = []
    for word in re.findall(r"[a-z][a-z0-9'\u2019]+", (text or "").lower()):
        word = word.strip("'\u2019")
        if len(word) < 3 or word in STOPWORDS:
            continue
        if word.endswith("ies") and len(word) > 4:
            word = word[:-3] + "y"
        elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
            word = word[:-1]
        tokens.append(word)
    return tokens


@dataclass
class Article:
    title: str
    path: str
    text: str
    counts: Counter


def load_articles(kb_dir: Path) -> list[Article]:
    articles: list[Article] = []
    if not kb_dir.is_dir():
        return articles
    for path in sorted(kb_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        title_match = re.search(r"^#\s+(.+)$", text, re.M)
        title = title_match.group(1).strip() if title_match else path.stem.replace("-", " ").capitalize()
        articles.append(Article(title=title, path=f"{kb_dir.name}/{path.name}", text=text,
                                counts=Counter(tokenize(text))))
    return articles


def _facts_query(facts: FactSheet) -> str:
    parts = [facts.feature_name] * 3 + [facts.one_liner]
    for name in ("how_it_works", "limits", "availability", "settings", "ui_text", "support_notes"):
        parts.extend(getattr(facts, name))
    parts.extend(change.description for change in facts.changes)
    return "\n".join(parts)


def _excerpt(text: str, terms: list[str]) -> str:
    """The body sentence that shares the most terms with the feature (titles skipped)."""
    best, best_hits = "", 0
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            clean = re.sub(r"[*_`]", "", sentence).strip().lstrip("-0123456789. ").strip()
            if len(clean) < 12:
                continue
            hits = len(set(tokenize(clean)) & set(terms))
            if hits > best_hits:
                best, best_hits = clean, hits
    return best[:180] + ("..." if len(best) > 180 else "")


def rank_articles(facts: FactSheet, articles: list[Article], top_k: int = 4,
                  min_score: float = 0.08) -> list[KBMatch]:
    if not articles:
        return []
    n_docs = len(articles)
    doc_freq: Counter = Counter()
    for article in articles:
        doc_freq.update(set(article.counts))

    def idf(term: str) -> float:
        return math.log((1 + n_docs) / (1 + doc_freq.get(term, 0))) + 1.0

    def vector(counts: Counter) -> dict[str, float]:
        return {term: (1 + math.log(count)) * idf(term) for term, count in counts.items()}

    query = vector(Counter(tokenize(_facts_query(facts))))
    query_norm = math.sqrt(sum(v * v for v in query.values()))
    if not query_norm:
        return []
    matches: list[KBMatch] = []
    for article in articles:
        doc = vector(article.counts)
        doc_norm = math.sqrt(sum(v * v for v in doc.values()))
        shared = [t for t in query if t in doc]
        if not doc_norm or not shared:
            continue
        score = sum(query[t] * doc[t] for t in shared) / (query_norm * doc_norm)
        shared.sort(key=lambda t: -(query[t] * doc[t]))
        matches.append(KBMatch(title=article.title, path=article.path, score=round(score, 3),
                               terms=shared[:6], excerpt=_excerpt(article.text, shared[:6])))
    matches.sort(key=lambda m: -m.score)
    return [m for m in matches if m.score >= min_score][:top_k]
