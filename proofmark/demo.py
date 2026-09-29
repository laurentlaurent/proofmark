"""Offline demo mode: rule-based extraction and template drafts.

This exists so anyone can click through the whole workflow without an API key,
and so the tests run deterministically. It only rearranges what the spec
already says, so the drafts are plain. It works best on specs with headings
such as "How it works", "Rules and limits", "Availability" and "Edge cases".

Some helpers here (code-name detection, version comparison, the gap
checklist) are also used in AI mode as deterministic backstops.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .schemas import Change, DocSettings, FactSheet, KBMatch, Sources

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
BULLET_RE = re.compile(r"^\s*(?:[-*+\u2022]|\d+[.)])\s+(.*)$")
TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{2,}")
CODE_NAME_RE = re.compile(r"\((?:internal(?: name)?|code ?name|project)\s*[:\-]?\s*([^)]+)\)", re.I)
CODE_NAME_LINE_RE = re.compile(r"^\s*(?:code ?name|internal name)\s*:\s*(.+)$", re.I | re.M)
TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")
BACKTICK_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]{2,})`")
SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
STRING_LITERAL_RE = re.compile(r"'((?:[^'\\\n]|\\.)*)'" + r'|"((?:[^"\\\n]|\\.)*)"')
CODE_CHARS_RE = re.compile(r"[(){};=&|<>\[\]]")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"\u201c(])")

# Heading keyword -> fact field. Order matters: first match wins.
FIELD_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("open_questions", ("open question", "open item", "unknown", "tbd", "to be decided", "to decide")),
    ("known_issues", ("known issue", "limitation", "caveat", "bug")),
    ("changes", ("what's new", "whats new", "what changed", "changelog", "changes")),
    ("ui_text", ("error message", "messages", "message", "copy", "ui text", "strings", "wording")),
    ("support_notes", ("edge case", "support", "troubleshoot", "faq", "failure", "errors")),
    ("how_it_works", ("how it works", "how to", "user flow", "flow", "journey", "steps", "experience",
                      "behaviour", "behavior", "usage")),
    ("availability", ("availability", "available", "rollout", "roll-out", "release", "launch", "platform",
                      "region", "eligib")),
    ("limits", ("rule", "limit", "requirement", "restriction", "constraint", "pricing", "fee", "cost")),
    ("settings", ("setting", "option", "configuration", "preference", "control")),
    ("value_to_user", ("why", "goal", "benefit", "value")),
    ("audience", ("audience", "who", "target", "persona")),
    ("one_liner", ("summary", "overview", "tl;dr", "tldr", "description", "what is", "introduction")),
]
IGNORED_HEADINGS = ("metric", "kpi", "success criteria", "analytics", "out of scope", "non-goal", "technical",
                    "implementation", "architecture", "appendix", "owner", "timeline", "problem", "background",
                    "motivation", "context", "risk")

GAP_CHECKLIST = [
    ("availability", "Which platforms, app versions and regions get this feature, and from when?"),
    ("how_it_works", "What exactly does the player do, step by step?"),
    ("limits", "Are there limits, fees or eligibility rules players should know about?"),
    ("support_notes", "What should players do if something goes wrong, and what can agents check?"),
    ("settings", "Can players turn this off or change how it works?"),
]


@dataclass
class Section:
    title: str
    level: int
    lines: list[str] = field(default_factory=list)


def clean_inline(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    text = re.sub(r"(?<![\w*])([*_])(?!\s)(.+?)(?<!\s)\1(?![\w*])", r"\2", text)
    text = text.replace("`", "")
    return re.sub(r"\s+", " ", text).strip()


def split_sections(markdown_text: str) -> tuple[str, list[Section]]:
    """Return the H1 title and the sections below it (code blocks are skipped)."""
    title = ""
    sections: list[Section] = []
    current = Section("", 0)
    in_code = False
    for raw in (markdown_text or "").splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        match = HEADING_RE.match(line)
        if match:
            level, text = len(match.group(1)), clean_inline(match.group(2))
            if level == 1 and not title:
                title = text
                continue
            if current.title or any(l.strip() for l in current.lines):
                sections.append(current)
            current = Section(text, level)
            continue
        current.lines.append(line)
    if current.title or any(l.strip() for l in current.lines):
        sections.append(current)
    return title, sections


def section_items(lines: list[str]) -> list[str]:
    """Bullets, numbered steps, table rows and paragraph sentences, in order."""
    items: list[str] = []
    paragraph: list[str] = []
    in_table = False
    header_seen = False

    def flush() -> None:
        if paragraph:
            items.extend(SENTENCE_SPLIT_RE.split(" ".join(paragraph)))
            paragraph.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
            in_table = False
            continue
        if stripped.startswith("|"):
            flush()
            if not in_table:
                in_table, header_seen = True, False
            if TABLE_SEPARATOR_RE.match(stripped):
                continue
            cells = [clean_inline(c) for c in stripped.strip("|").split("|")]
            if not header_seen:
                header_seen = True
                continue
            items.append(" \u2014 ".join(c for c in cells if c))
            continue
        in_table = False
        bullet = BULLET_RE.match(line)
        if bullet:
            flush()
            items.append(bullet.group(1))
            continue
        paragraph.append(stripped)
    flush()
    cleaned = [clean_inline(item) for item in items]
    return [item for item in cleaned if item]


def field_for_heading(heading: str) -> str | None:
    lowered = heading.lower()
    if any(word in lowered for word in IGNORED_HEADINGS):
        return None
    for field_name, keywords in FIELD_RULES:
        if any(keyword in lowered for keyword in keywords):
            return field_name
    return None


def detect_internal_terms(sources: Sources) -> list[str]:
    """Code names, ticket IDs and flags: things that must never reach players."""
    terms: list[str] = []
    spec = sources.spec or ""
    title_line = next((l for l in spec.splitlines() if l.startswith("# ")), "")
    terms += [m.strip() for m in CODE_NAME_RE.findall(title_line)]
    terms += [m.strip() for m in CODE_NAME_LINE_RE.findall(spec)]
    terms += TICKET_RE.findall(spec + "\n" + (sources.previous_spec or ""))
    terms += BACKTICK_RE.findall(spec)
    added = "\n".join(l[1:] for l in (sources.code_diff or "").splitlines()
                      if l.startswith("+") and not l.startswith("+++"))
    terms += SNAKE_RE.findall(added)
    out: list[str] = []
    for term in terms:
        term = term.strip()
        if term and term.lower() not in {t.lower() for t in out}:
            out.append(term)
    return out[:15]


def diff_ui_text(code_diff: str) -> tuple[list[str], list[str]]:
    """Player-visible strings added and removed in a unified diff."""
    added: list[str] = []
    removed: list[str] = []
    for line in (code_diff or "").splitlines():
        if line.startswith(("+++", "---")) or line[:1] not in ("+", "-"):
            continue
        target = added if line.startswith("+") else removed
        for match in STRING_LITERAL_RE.finditer(line[1:]):
            text = (match.group(1) if match.group(1) is not None else match.group(2) or "").strip()
            text = text.replace("\\'", "'").replace('\\"', '"')
            looks_like_copy = (len(text) >= 8 and " " in text and text[:1].isupper()
                               and re.search(r"[A-Za-z]{3}", text) and not CODE_CHARS_RE.search(text))
            if looks_like_copy and text not in target:
                target.append(text)
    return added, [text for text in removed if text not in added]


COMPARED_FIELDS = {"how_it_works", "limits", "availability", "settings", "ui_text", "support_notes",
                   "known_issues", "audience"}


def _all_items(markdown_text: str) -> list[str]:
    """Behavioural items only: summaries and goals change wording without changing behaviour."""
    _, sections = split_sections(markdown_text)
    items: list[str] = []
    for section in sections:
        if field_for_heading(section.title) in COMPARED_FIELDS:
            items.extend(section_items(section.lines))
    return items


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9$%. ]+", " ", text.lower()).strip()


def compare_versions(previous_md: str, current_md: str, limit: int = 12) -> list[Change]:
    """Match bullets between two versions of a spec: added, changed or removed."""
    old_items, new_items = _all_items(previous_md), _all_items(current_md)
    # Global greedy matching: the most similar pairs are matched first.
    pairs = sorted(((SequenceMatcher(None, _norm(new), _norm(old)).ratio(), n, o)
                    for n, new in enumerate(new_items) for o, old in enumerate(old_items)), reverse=True)
    pair_of_new: dict[int, tuple[int, float]] = {}
    used_old: set[int] = set()
    for ratio, n, o in pairs:
        if ratio < 0.55:
            break
        if n not in pair_of_new and o not in used_old:
            pair_of_new[n] = (o, ratio)
            used_old.add(o)
    changes: list[Change] = []
    for n, item in enumerate(new_items):
        if n not in pair_of_new:
            changes.append(Change(kind="added", description=item))
            continue
        o, ratio = pair_of_new[n]
        if ratio < 0.9:
            changes.append(Change(kind="changed", description=f"{item} (before: {old_items[o]})"))
    changes += [Change(kind="removed", description=old) for o, old in enumerate(old_items) if o not in used_old]
    return changes[:limit]


def extract_facts(sources: Sources) -> FactSheet:
    title, sections = split_sections(sources.spec)
    data: dict[str, list[str]] = {}
    for section in sections:
        field_name = field_for_heading(section.title)
        if not field_name:
            continue
        data.setdefault(field_name, []).extend(section_items(section.lines))

    summary = data.pop("one_liner", [])
    one_liner = summary[0] if summary else ""
    value = summary[1:] + data.get("value_to_user", [])
    name = CODE_NAME_RE.sub("", title).strip() or "Untitled feature"
    name = re.sub(r"^(?:feature spec|spec|prd)\s*[:\-\u2013]\s*", "", name, flags=re.I).strip()

    ui_added, ui_removed = diff_ui_text(sources.code_diff)
    ui_text = data.get("ui_text", []) + [f"\u201c{text}\u201d" for text in ui_added]

    changes = [Change(kind="changed", description=item) for item in data.get("changes", [])]
    if sources.previous_spec.strip():
        changes += compare_versions(sources.previous_spec, sources.spec)
    changes += [Change(kind="removed", description=f"Message no longer shown: \u201c{text}\u201d")
                for text in ui_removed]

    facts = FactSheet(
        feature_name=name,
        one_liner=one_liner,
        audience=data.get("audience", []),
        value_to_user=value,
        availability=data.get("availability", []),
        how_it_works=data.get("how_it_works", []),
        settings=data.get("settings", []),
        limits=data.get("limits", []),
        known_issues=data.get("known_issues", []),
        support_notes=data.get("support_notes", []),
        ui_text=ui_text,
        changes=changes,
        open_questions=[re.sub(r"\s*\bTBD\b\.?\s*$", "", q).strip() for q in data.get("open_questions", [])],
        internal_terms=detect_internal_terms(sources),
    )
    facts.open_questions += [q for field_name, q in GAP_CHECKLIST if not getattr(facts, field_name)]
    return facts


# --------------------------------------------------------------------------- templates


def _sentence(text: str) -> str:
    text = text.strip()
    return text if re.search(r"(?:[.!?\u201d\"]|[.!?][)\u201d\"])$", text) else text + "."


def _words(text: str, limit: int) -> str:
    words = text.split()
    return text if len(words) <= limit else " ".join(words[:limit]).rstrip(",;:") + "..."


def _bullets(items: list[str]) -> list[str]:
    return [f"- {_sentence(item)}" for item in items]


def help_article(f: FactSheet) -> str:
    name = f.feature_name
    title = f"How to use {name}" if f.how_it_works else f"What is {name}?"
    out = [f"# {title}", "", _sentence(f.one_liner or f"{name} is now available."), ""]
    if f.how_it_works:
        out += [f"## How to use {name}", ""]
        out += [f"{i}. {_sentence(step)}" for i, step in enumerate(f.how_it_works, 1)] + [""]
    good_to_know = f.availability + f.limits + f.settings + [c.answer for c in f.clarifications]
    if good_to_know:
        out += ["## Good to know", ""] + _bullets(good_to_know) + [""]
    trouble = f.support_notes + f.known_issues
    if trouble:
        out += ["## If something goes wrong", ""] + _bullets(trouble) + [""]
    out += ["## Still need help?", "", "Contact our support team and we'll be happy to help."]
    return "\n".join(out).strip() + "\n"


def faq(f: FactSheet) -> str:
    name = f.feature_name
    pairs: list[tuple[str, str]] = []
    if f.one_liner:
        pairs.append((f"What is {name}?", _sentence(f.one_liner)))
    if f.availability:
        pairs.append((f"Who can use {name}?", " ".join(_sentence(x) for x in f.availability[:3])))
    if f.how_it_works:
        pairs.append((f"How do I use {name}?", " ".join(_sentence(x) for x in f.how_it_works[:5])))
    if f.limits:
        pairs.append(("Are there any limits or fees?", " ".join(_sentence(x) for x in f.limits[:4])))
    if f.settings:
        pairs.append(("Can I change how it works?", " ".join(_sentence(x) for x in f.settings[:3])))
    trouble = f.support_notes + f.known_issues
    if trouble:
        pairs.append(("What if something goes wrong?", " ".join(_sentence(x) for x in trouble[:3])))
    if f.changes:
        pairs.append(("What's different from before?",
                      " ".join(_sentence(c.description) for c in f.changes[:3])))
    pairs += [(c.question if c.question.endswith("?") else c.question + "?", _sentence(c.answer))
              for c in f.clarifications]
    out = [f"# {name}: frequently asked questions", ""]
    for question, answer in pairs:
        out += [f"### {question}", answer, ""]
    return "\n".join(out).strip() + "\n"


def release_notes(f: FactSheet) -> str:
    prefix = {"added": "New", "changed": "Improved", "fixed": "Fixed", "removed": "Changed"}
    out = [f"# What's new: {f.feature_name}", "", _words(_sentence(f.one_liner or f.feature_name), 50), "",
           "## Details", ""]
    if f.changes:
        details = [f"- {prefix[c.kind]}: {_sentence(re.sub(r' [(]before:.*[)]$', '', c.description))}"
                   for c in f.changes[:6]]
    else:
        details = [f"- New: {_sentence(item)}" for item in (f.value_to_user + f.limits)[:5]]
    out += details or ["- New: " + _sentence(f.feature_name)]
    return "\n".join(out).strip() + "\n"


def agent_brief(f: FactSheet) -> str:
    out = [f"# Agent brief: {f.feature_name}", "",
           f"**In short:** {_sentence(f.one_liner or f.feature_name)}", ""]

    def section(heading: str, items: list[str], numbered: bool = False) -> None:
        if not items:
            return
        out.extend([f"## {heading}", ""])
        out.extend(f"{i}. {_sentence(x)}" if numbered else f"- {_sentence(x)}" for i, x in enumerate(items, 1))
        out.append("")

    section("What changed", [f"{c.kind.capitalize()}: {c.description}" for c in f.changes])
    section("Who gets it", f.audience + f.availability)
    section("How it works for the player", f.how_it_works, numbered=True)
    section("Limits and rules", f.limits + f.settings)
    section("Known issues and workarounds", f.known_issues)
    section("Troubleshooting", f.support_notes, numbered=True)
    section("What players see", f.ui_text)
    section("Confirmed by the product team", [f"{c.question} {c.answer}" for c in f.clarifications])
    out += ["## Suggested replies", "",
            f"> Thanks for reaching out! {_sentence(f.one_liner or f.feature_name)} "
            "Let me know if you'd like me to walk you through it.", "",
            "> Sorry this didn't work as expected. I've checked your account and will "
            "keep you posted as soon as I have an update.", ""]
    section("Not confirmed yet", f.open_questions)
    if f.internal_terms:
        out += ["## Internal names", "", "Never use these with players: " + ", ".join(f.internal_terms) + ".", ""]
    return "\n".join(out).strip() + "\n"


def whats_changed(f: FactSheet, kb_matches: list[KBMatch]) -> str:
    out = [f"# What changed: {f.feature_name}", "",
           "Changes found by comparing this spec with the previous version.", "",
           "| Area | Before | Now | What players will notice |", "| --- | --- | --- | --- |"]
    for change in f.changes:
        text = change.description.replace("|", "/")
        before_match = re.search(r"^(.*) \(before: (.*)\)$", text)
        if before_match:
            before, now = before_match.group(2), before_match.group(1)
        elif change.kind == "removed":
            before, now = text, "Removed"
        else:
            before, now = "Not available", text
        out.append(f"| {change.kind.capitalize()} | {before} | {now} | Check the Help Centre wording |")
    out += ["", "## Help Centre articles to update", ""]
    if kb_matches:
        out += [f"- **{m.title}** mentions {', '.join(m.terms[:4])}. Review it against the new behaviour."
                for m in kb_matches]
    else:
        out.append("No existing article seems affected.")
    return "\n".join(out).strip() + "\n"


def render_doc(doc_type: str, facts: FactSheet, settings: DocSettings, kb_matches: list[KBMatch]) -> str:
    if doc_type == "help_article":
        return help_article(facts)
    if doc_type == "faq":
        return faq(facts)
    if doc_type == "release_notes":
        return release_notes(facts)
    if doc_type == "agent_brief":
        return agent_brief(facts)
    if doc_type == "whats_changed":
        return whats_changed(facts, kb_matches)
    raise ValueError(f"Unknown document type: {doc_type}")
