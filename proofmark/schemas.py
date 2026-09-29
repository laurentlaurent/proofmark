"""Data shapes shared by the pipeline, the API and the UI.

Model output is messy (a string where a list was asked for, objects instead of
strings, bullets left in). The validators below coerce it into clean lists so
one sloppy answer does not break the whole run.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

DocType = Literal["help_article", "faq", "release_notes", "agent_brief", "whats_changed"]
ChangeKind = Literal["added", "changed", "removed", "fixed"]
CheckStatus = Literal["pass", "warn", "fail", "info"]

DOC_TYPES: dict[str, dict[str, str]] = {
    "help_article": {"label": "Help Centre article", "reader": "players",
                     "blurb": "Step-by-step article for players"},
    "faq": {"label": "FAQ", "reader": "players", "blurb": "Questions in players' own words"},
    "release_notes": {"label": "Release notes", "reader": "players", "blurb": "Store blurb and changelog"},
    "agent_brief": {"label": "Agent brief", "reader": "agents",
                    "blurb": "Internal: troubleshooting and ready replies"},
    "whats_changed": {"label": "What changed", "reader": "agents",
                      "blurb": "Internal: before and after, articles to update"},
}
PLAYER_DOCS = {key for key, meta in DOC_TYPES.items() if meta["reader"] == "players"}

_BULLET = re.compile(r"^\s*(?:[-*+\u2022]|\d+[.)])\s+")
_KIND_ALIASES = {"new": "added", "add": "added", "improved": "changed", "update": "changed",
                 "updated": "changed", "change": "changed", "remove": "removed", "fix": "fixed"}


def as_items(value: Any) -> list[str]:
    """Coerce anything list-like into a clean, de-duplicated list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.splitlines()
    elif isinstance(value, dict):
        parts = [f"{k}: {v}" for k, v in value.items() if v]
    elif isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, dict):
                text = (item.get("text") or item.get("description") or item.get("value")
                        or item.get("question") or "; ".join(str(v) for v in item.values() if v))
                parts.append(str(text))
            else:
                parts.append(str(item))
    else:
        parts = [str(value)]
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = _BULLET.sub("", part).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        return " ".join(as_items(value)) or default
    return str(value).strip() or default


class Change(BaseModel):
    kind: ChangeKind = "changed"
    description: str

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, value: Any) -> str:
        kind = str(value or "changed").strip().lower()
        kind = _KIND_ALIASES.get(kind, kind)
        return kind if kind in {"added", "changed", "removed", "fixed"} else "changed"


class Clarification(BaseModel):
    question: str
    answer: str


class FactSheet(BaseModel):
    """The single source of truth every draft is written from."""

    feature_name: str = "Untitled feature"
    one_liner: str = ""
    audience: list[str] = Field(default_factory=list)
    value_to_user: list[str] = Field(default_factory=list)
    availability: list[str] = Field(default_factory=list)
    how_it_works: list[str] = Field(default_factory=list)
    settings: list[str] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)
    known_issues: list[str] = Field(default_factory=list)
    support_notes: list[str] = Field(default_factory=list)
    ui_text: list[str] = Field(default_factory=list)
    changes: list[Change] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    internal_terms: list[str] = Field(default_factory=list)
    clarifications: list[Clarification] = Field(default_factory=list)

    @field_validator("feature_name", mode="before")
    @classmethod
    def _name(cls, value: Any) -> str:
        return _as_text(value, "Untitled feature")

    @field_validator("one_liner", mode="before")
    @classmethod
    def _one_liner(cls, value: Any) -> str:
        return _as_text(value)

    @field_validator("audience", "value_to_user", "availability", "how_it_works", "settings", "limits",
                     "known_issues", "support_notes", "ui_text", "open_questions", "internal_terms",
                     mode="before")
    @classmethod
    def _lists(cls, value: Any) -> list[str]:
        return as_items(value)

    @field_validator("changes", mode="before")
    @classmethod
    def _changes(cls, value: Any) -> list[dict]:
        if value is None:
            return []
        if isinstance(value, str):
            value = value.splitlines()
        out: list[dict] = []
        for item in value:
            if isinstance(item, Change):
                out.append(item.model_dump())
            elif isinstance(item, dict):
                description = str(item.get("description") or item.get("text") or item.get("change") or "").strip()
                if description:
                    out.append({"kind": item.get("kind") or item.get("type") or "changed",
                                "description": description})
            elif isinstance(item, str) and item.strip():
                text = _BULLET.sub("", item).strip()
                match = re.match(r"^(added|changed|removed|fixed|new|improved)\s*[:\-\u2013]\s*(.+)$", text, re.I)
                if match:
                    out.append({"kind": match.group(1), "description": match.group(2).strip()})
                else:
                    out.append({"kind": "changed", "description": text})
        return out

    @field_validator("clarifications", mode="before")
    @classmethod
    def _clarifications(cls, value: Any) -> list[dict]:
        if value is None:
            return []
        out: list[dict] = []
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, Clarification):
                out.append(item.model_dump())
            elif isinstance(item, dict) and item.get("question") and item.get("answer"):
                out.append({"question": str(item["question"]).strip(), "answer": str(item["answer"]).strip()})
            elif isinstance(item, str) and "\u2014" in item:
                question, answer = item.split("\u2014", 1)
                out.append({"question": question.strip(), "answer": answer.strip()})
        return out

    def as_text(self) -> str:
        """Flat text, used for number grounding and article matching."""
        parts = [self.feature_name, self.one_liner]
        for name in ("audience", "value_to_user", "availability", "how_it_works", "settings", "limits",
                     "known_issues", "support_notes", "ui_text", "open_questions"):
            parts.extend(getattr(self, name))
        parts.extend(change.description for change in self.changes)
        parts.extend(f"{c.question} {c.answer}" for c in self.clarifications)
        return "\n".join(p for p in parts if p)


class Sources(BaseModel):
    spec: str = ""
    previous_spec: str = ""
    code_diff: str = ""
    screenshots: list[str] = Field(default_factory=list, description="Images as data: URLs")

    def as_text(self) -> str:
        return "\n".join(part for part in (self.spec, self.previous_spec, self.code_diff) if part)


class DocSettings(BaseModel):
    product: str = "Blitz"
    tone: Literal["friendly", "neutral", "playful"] = "friendly"
    language: str = "English"

    @property
    def is_english(self) -> bool:
        return self.language.strip().lower().startswith("en")


class LLMChoice(BaseModel):
    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    vision: bool | None = None


class KBMatch(BaseModel):
    title: str
    path: str
    score: float
    terms: list[str] = Field(default_factory=list)
    excerpt: str = ""


class Check(BaseModel):
    id: str
    label: str
    status: CheckStatus
    detail: str = ""
    items: list[str] = Field(default_factory=list)


class Approval(BaseModel):
    """A reviewer's sign-off on one exact version of a draft."""

    by: str
    at: str = ""
    note: str = ""
    sha256: str = Field("", description="SHA-256 of the Markdown that was approved")


class Draft(BaseModel):
    doc_type: DocType
    title: str
    markdown: str
    html: str = ""
    checks: list[Check] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)
    approval: Approval | None = None


class ExtractRequest(BaseModel):
    sources: Sources
    settings: DocSettings = Field(default_factory=DocSettings)
    llm: LLMChoice = Field(default_factory=LLMChoice)


class ExtractResponse(BaseModel):
    facts: FactSheet
    warnings: list[str] = Field(default_factory=list)
    number_issues: list[str] = Field(default_factory=list)
    kb_matches: list[KBMatch] = Field(default_factory=list)
    version_diff: str = ""
    meta: dict = Field(default_factory=dict)


class GenerateRequest(BaseModel):
    facts: FactSheet
    doc_type: DocType
    settings: DocSettings = Field(default_factory=DocSettings)
    llm: LLMChoice = Field(default_factory=LLMChoice)
    sources: Sources | None = None
    kb_matches: list[KBMatch] = Field(default_factory=list)
    instruction: str = ""
    fact_check: bool = True


class CheckRequest(BaseModel):
    doc_type: DocType
    markdown: str
    facts: FactSheet
    sources: Sources | None = None
    settings: DocSettings = Field(default_factory=DocSettings)


class CheckResponse(BaseModel):
    title: str
    html: str
    checks: list[Check]


class ExportRequest(BaseModel):
    facts: FactSheet
    drafts: list[Draft]
    settings: DocSettings = Field(default_factory=DocSettings)
    sources: Sources | None = None
    extract_meta: dict = Field(default_factory=dict)
