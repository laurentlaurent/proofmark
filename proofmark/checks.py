"""Deterministic quality checks ("proof marks") run on every draft.

They are cheap, explainable and never hallucinate, so they complement the
AI fact-check rather than replace it:
- reading level (Flesch-Kincaid grade, English only)
- internal jargon and code names in player-facing text
- numbers that do not appear in the sources (the cheapest hallucination detector)
- leftover placeholders
- structure expected for each document type
- open questions that are still unanswered
"""
from __future__ import annotations

import re

from .schemas import PLAYER_DOCS, Check, DocSettings, FactSheet, Sources

GENERIC_JARGON = [
    "api", "backend", "back-end", "frontend", "front-end", "endpoint", "sdk", "feature flag", "rollout",
    "a/b test", "psp", "acquirer", "bin", "3ds", "3-d secure", "tokenization", "tokenisation", "webhook",
    "latency", "deploy", "deployment", "staging", "database", "payload", "null", "boolean", "config",
    "jira", "sprint", "squad", "stakeholder", "kpi", "metric", "funnel", "edge case",
]

WORD_NUMBERS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12"}

_UNIT = (r"(?:\s?%|\s?percent\b|\s?(?:business\s+)?(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?"
         r"|weeks?|months?|years?)\b)")
NUMBER_RE = re.compile(
    rf"(?<![\w.])(?P<cur>[$\u20ac\u00a3]\s?)?(?P<num>\d{{1,3}}(?:,\d{{3}})+|\d+)(?P<dec>\.\d+)?(?![\w])(?P<unit>{_UNIT})?",
    re.I,
)
SOURCE_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
LIST_MARKER_RE = re.compile(r"^\s*\d+[.)]\s+")
PLACEHOLDER_RE = re.compile(
    r"\[(?:tbd|tbc|todo|x{2,}|insert[^\]]*|placeholder[^\]]*|link[^\]]*|name[^\]]*)\]|\{\{[^}]*\}\}|lorem ipsum",
    re.I,
)
PLACEHOLDER_CAPS_RE = re.compile(r"\b(?:TBD|TBC|TODO|FIXME|XXX)\b")


def _normalise_number(raw: str) -> str:
    value = raw.replace(",", "")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value


def source_numbers(text: str) -> set[str]:
    found = {_normalise_number(match) for match in SOURCE_NUMBER_RE.findall(text or "")}
    lowered = (text or "").lower()
    for word, digit in WORD_NUMBERS.items():
        if re.search(rf"\b{word}\b", lowered):
            found.add(digit)
    return found


def ungrounded_numbers(text: str, sources_text: str, limit: int = 10) -> list[str]:
    """Numbers (money, percentages, durations, 2+ digit values) that are not in the sources."""
    known = source_numbers(sources_text)
    missing: list[str] = []
    in_code = False
    for line in (text or "").splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        line = LIST_MARKER_RE.sub("", line)
        for match in NUMBER_RE.finditer(line):
            num = match.group("num")
            dec = match.group("dec") or ""
            has_context = bool(match.group("cur") or match.group("unit") or dec)
            if not has_context and len(num.replace(",", "")) < 2:
                continue  # single digits without context are too noisy to check
            if _normalise_number(num + dec) in known:
                continue
            token = match.group(0).strip()
            if token not in missing:
                missing.append(token)
            if len(missing) >= limit:
                return missing
    return missing


def strip_markdown(md: str) -> str:
    text = re.sub(r"```.*?```", " ", md or "", flags=re.S)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    sentences = []
    for line in text.splitlines():
        s = line.strip()
        if not s or re.match(r"^\|?\s*:?-{2,}", s):
            continue
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"^(?:[-*+>]|\d+[.)])\s+", "", s)
        s = s.replace("|", " ")
        s = re.sub(r"[*_]{1,3}", "", s).strip()
        if s and not re.search(r"[.!?:]$", s):
            s += "."  # headings and bullets read as separate sentences
        sentences.append(s)
    return " ".join(sentences)


def _syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", w)))


def readability(md: str) -> dict | None:
    text = strip_markdown(md)
    words = re.findall(r"[A-Za-z][A-Za-z'\u2019-]*", text)
    sentences = [s for s in re.split(r"[.!?:]+(?:\s|$)", text) if re.search(r"[A-Za-z]", s)]
    if not words or not sentences:
        return None
    syllables = sum(_syllables(w) for w in words)
    words_per_sentence = len(words) / len(sentences)
    syllables_per_word = syllables / len(words)
    return {
        "words": len(words),
        "sentences": len(sentences),
        "words_per_sentence": round(words_per_sentence, 1),
        "reading_ease": round(206.835 - 1.015 * words_per_sentence - 84.6 * syllables_per_word, 1),
        "grade": round(0.39 * words_per_sentence + 11.8 * syllables_per_word - 15.59, 1),
    }


def _find_terms(md: str, terms: list[str]) -> list[str]:
    found = []
    for term in terms:
        term = term.strip()
        if len(term) < 2:
            continue
        pattern = rf"(?<![\w-]){re.escape(term)}(?![\w-])"
        if re.search(pattern, md, re.I) and term not in found:
            found.append(term)
    return found


def check_reading_level(md: str, doc_type: str, settings: DocSettings) -> Check:
    if not settings.is_english:
        return Check(id="reading_level", label="Reading level", status="info",
                     detail="Measured for English only.")
    stats = readability(md)
    if not stats:
        return Check(id="reading_level", label="Reading level", status="info", detail="Not enough text to measure.")
    grade, words = stats["grade"], stats["words"]
    detail = f"Grade {grade:g}, {words} words, {stats['words_per_sentence']:g} words per sentence."
    if doc_type not in PLAYER_DOCS:
        return Check(id="reading_level", label="Reading level", status="info", detail=detail)
    status = "pass" if grade <= 8 else "warn" if grade <= 11 else "fail"
    if status != "pass":
        detail += " Aim for grade 8 or below: shorter sentences, simpler words."
    return Check(id="reading_level", label="Reading level", status=status, detail=detail)


def check_jargon(md: str, doc_type: str, facts: FactSheet) -> Check:
    if doc_type not in PLAYER_DOCS:
        return Check(id="jargon", label="Internal terms", status="info",
                     detail="Internal document: internal terms are allowed.")
    code_names = _find_terms(md, facts.internal_terms)
    generic = [t for t in _find_terms(md, GENERIC_JARGON) if t not in code_names]
    if code_names:
        return Check(id="jargon", label="Internal terms", status="fail",
                     detail="Internal names from the sources appear in a player-facing document.",
                     items=code_names + generic)
    if generic:
        return Check(id="jargon", label="Internal terms", status="warn",
                     detail="Engineering words players may not understand.", items=generic)
    return Check(id="jargon", label="Internal terms", status="pass", detail="No internal terms or code names.")


def check_numbers(md: str, facts: FactSheet, sources: Sources | None) -> Check:
    grounding = facts.as_text() + "\n" + (sources.as_text() if sources else "")
    missing = ungrounded_numbers(md, grounding)
    if missing:
        return Check(id="numbers", label="Numbers match sources", status="warn",
                     detail="These numbers do not appear in the sources or the fact sheet. Verify them.",
                     items=missing)
    return Check(id="numbers", label="Numbers match sources", status="pass",
                 detail="Every amount, percentage and duration appears in the sources.")


def check_placeholders(md: str) -> Check:
    found = sorted({m.group(0) for m in PLACEHOLDER_RE.finditer(md)} |
                   {m.group(0) for m in PLACEHOLDER_CAPS_RE.finditer(md)})
    if found:
        return Check(id="placeholders", label="Placeholders", status="fail",
                     detail="Fill these in before publishing.", items=found)
    return Check(id="placeholders", label="Placeholders", status="pass", detail="Nothing left to fill in.")


def _first_paragraph_after_title(md: str) -> str:
    lines = md.strip().splitlines()[1:]
    para: list[str] = []
    for line in lines:
        if not line.strip():
            if para:
                break
            continue
        if line.lstrip().startswith(("#", "-", "*", "|")):
            break
        para.append(line.strip())
    return " ".join(para)


def check_structure(md: str, doc_type: str, settings: DocSettings) -> Check:
    problems: list[str] = []
    lines = [line for line in md.strip().splitlines()]
    title = lines[0][2:].strip() if lines and lines[0].startswith("# ") else ""
    if not title:
        problems.append("Start with a '# ' title line.")
    word_count = len(re.findall(r"\w+", strip_markdown(md)))
    if doc_type == "help_article":
        if len(title) > 70:
            problems.append(f"Title is {len(title)} characters; keep it under 70.")
        if not re.search(r"^\s*1[.)]\s", md, re.M):
            problems.append("No numbered steps.")
        if word_count > 600:
            problems.append(f"{word_count} words; aim for 150 to 400.")
    elif doc_type == "faq":
        questions = len(re.findall(r"^#{2,4}\s+.+\?\s*$", md, re.M))
        if questions < 4:
            problems.append(f"{questions} questions; aim for 5 to 8.")
    elif doc_type == "release_notes":
        blurb = _first_paragraph_after_title(md)
        blurb_words = len(re.findall(r"\w+", blurb))
        if not blurb:
            problems.append("Missing the short blurb under the title.")
        elif blurb_words > 50:
            problems.append(f"Blurb is {blurb_words} words; keep it to 50 for the store.")
    elif doc_type == "agent_brief" and settings.is_english:
        for heading in ("Troubleshooting", "Suggested replies"):
            if not re.search(rf"^##\s+{heading}", md, re.M | re.I):
                problems.append(f"Missing the '{heading}' section.")
    elif doc_type == "whats_changed" and "|" not in md:
        problems.append("Missing the before and after table.")
    if problems:
        return Check(id="structure", label="Structure", status="warn", detail="Differs from the template.",
                     items=problems)
    return Check(id="structure", label="Structure", status="pass", detail="Follows the template.")


def check_open_questions(doc_type: str, facts: FactSheet) -> Check:
    count = len(facts.open_questions)
    if not count:
        return Check(id="open_questions", label="Open questions", status="pass", detail="All answered.")
    status = "warn" if doc_type in PLAYER_DOCS else "info"
    noun, pronoun = ("question is", "it") if count == 1 else ("questions are", "them")
    return Check(id="open_questions", label="Open questions", status=status,
                 detail=f"{count} {noun} still open in the fact sheet. Answer {pronoun} before publishing.",
                 items=facts.open_questions[:8])


def check_ai_review(unsupported: list[dict] | None, note: str = "") -> Check:
    if unsupported is None:
        return Check(id="ai_review", label="AI fact-check", status="info", detail=note or "Not run.")
    if not unsupported:
        return Check(id="ai_review", label="AI fact-check", status="pass",
                     detail="Every statement is backed by the fact sheet.")
    items = []
    for entry in unsupported[:10]:
        if isinstance(entry, dict):
            claim = str(entry.get("claim", "")).strip()
            why = str(entry.get("why", "")).strip()
            items.append(f"\u201c{claim}\u201d: {why}" if why else f"\u201c{claim}\u201d")
        else:
            items.append(str(entry))
    return Check(id="ai_review", label="AI fact-check", status="warn",
                 detail=f"{len(items)} statement(s) need a source or a fix.", items=items)


def run_all(md: str, doc_type: str, facts: FactSheet, sources: Sources | None, settings: DocSettings,
            unsupported: list[dict] | None = None, review_note: str = "") -> list[Check]:
    return [
        check_ai_review(unsupported, review_note),
        check_numbers(md, facts, sources),
        check_jargon(md, doc_type, facts),
        check_placeholders(md),
        check_reading_level(md, doc_type, settings),
        check_structure(md, doc_type, settings),
        check_open_questions(doc_type, facts),
    ]
