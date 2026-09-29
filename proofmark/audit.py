"""Audit trail: what left Proofmark, based on what, checked how, approved by whom.

Every export produces an audit record. It is saved inside the bundle
(audit.json) and appended to a local append-only log (audit/audit-log.jsonl),
with a copy of the bundle kept next to it. Each log entry carries the hash of
the previous entry, so editing, removing or reordering history is detectable
with `python -m proofmark audit --verify`.

Checks are re-run on the final text at export time rather than trusted from
the browser, so the record reflects what was actually exported.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

SCHEMA = "proofmark.audit/1"
GENESIS = "0" * 64
DATA_URL_RE = re.compile(r"^data:(image/[a-z0-9.+-]+);base64,(.+)$", re.I | re.S)
_WRITE_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_image(data_url: str) -> tuple[str, bytes] | None:
    """(mime type, bytes) for an image data: URL, or None if it is not one."""
    match = DATA_URL_RE.match(data_url or "")
    if not match:
        return None
    try:
        return match.group(1).lower(), base64.b64decode(match.group(2))
    except (binascii.Error, ValueError):
        return None


def overall_status(found_checks) -> str:
    statuses = {c.status for c in found_checks}
    return "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"


def final_checks(draft, facts, sources, settings):
    """Deterministic checks re-run on the exported text; the AI fact-check is kept
    only if the text is exactly what the model drafted and was checked."""
    from . import checks  # local import: checks has no dependency on this module

    sha = sha256_text(draft.markdown)
    generated = draft.meta.get("generated_sha256")
    edited = (generated != sha) if generated else None
    note = "Edited after drafting, so not fact-checked again." if edited else "Not run."
    found = checks.run_all(draft.markdown, draft.doc_type, facts, sources, settings, None, note)
    ai_review = next((c for c in draft.checks if c.id == "ai_review"), None)
    if edited is False and ai_review is not None:
        found[0] = ai_review
    return found, edited


def approval_entry(draft, final_sha: str) -> dict | None:
    approval = draft.approval
    if approval is None or not approval.by.strip():
        return None
    valid = not approval.sha256 or approval.sha256 == final_sha
    return {"by": approval.by.strip(), "at": approval.at, "note": approval.note.strip(), "valid": valid,
            "problem": "" if valid else "The draft changed after it was approved, so the approval does not count."}


def build_record(facts, drafts, settings, sources=None, extract_meta: dict | None = None) -> dict:
    """The audit record for one export. Raises UserInputError for an approval that
    accepts failing checks without saying why."""
    from .errors import UserInputError
    from .schemas import DOC_TYPES

    extract_meta = extract_meta or {}
    documents = []
    for draft in drafts:
        found, edited = final_checks(draft, facts, sources, settings)
        sha = sha256_text(draft.markdown)
        status = overall_status(found)
        approval = approval_entry(draft, sha)
        if approval and approval["valid"] and status == "fail" and not approval["note"]:
            raise UserInputError(f"\u201c{draft.title}\u201d was approved with failing checks but without a note. "
                                 "Add the reason to the approval, or withdraw it.")
        documents.append({
            "doc_type": draft.doc_type,
            "title": draft.title,
            "reader": DOC_TYPES[draft.doc_type]["reader"],
            "sha256": sha,
            "drafted_by": {key: draft.meta.get(key) for key in ("provider", "model", "prompt_version")},
            "drafted_at": draft.meta.get("drafted_at"),
            "redraft_note": draft.meta.get("instruction", ""),
            "edited_after_drafting": edited,
            "status": status,
            "checks": [{"id": c.id, "status": c.status, "detail": c.detail, "items": c.items} for c in found],
            "approval": approval,
        })

    screenshots = []
    for url in (sources.screenshots if sources else []):
        decoded = decode_image(url)
        if decoded:
            screenshots.append({"type": decoded[0], "bytes": len(decoded[1]), "sha256": sha256_bytes(decoded[1])})

    def source_entry(text: str) -> dict | None:
        return {"sha256": sha256_text(text), "characters": len(text)} if text and text.strip() else None

    approved = [d for d in documents if d["approval"] and d["approval"]["valid"]]
    return {
        "schema": SCHEMA,
        "id": uuid.uuid4().hex[:12],
        "exported_at": now_iso(),
        "proofmark_version": __version__,
        "feature": facts.feature_name,
        "settings": settings.model_dump(),
        "sources": {
            "spec": source_entry(sources.spec) if sources else None,
            "previous_spec": source_entry(sources.previous_spec) if sources else None,
            "code_diff": source_entry(sources.code_diff) if sources else None,
            "screenshots": screenshots,
        },
        "fact_sheet": {
            "sha256": sha256_text(facts.model_dump_json(indent=2)),
            "extracted_by": {key: extract_meta.get(key) for key in ("provider", "model", "prompt_version")},
            "extracted_at": extract_meta.get("extracted_at"),
            "open_questions": list(facts.open_questions),
            "confirmed_answers": [c.model_dump() for c in facts.clarifications],
        },
        "documents": documents,
        "summary": {
            "documents": len(documents),
            "approved": len(approved),
            "approved_with_warnings": sum(1 for d in approved if d["status"] == "warn"),
            "approved_with_failures": sum(1 for d in approved if d["status"] == "fail"),
            "invalid_approvals": sum(1 for d in documents if d["approval"] and not d["approval"]["valid"]),
            "open_questions": len(facts.open_questions),
        },
    }


def _entry_hash(entry: dict) -> str:
    body = {key: value for key, value in entry.items() if key != "entry_hash"}
    return sha256_text(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


class AuditLog:
    """Append-only JSON Lines log, hash-chained, with a copy of every exported bundle."""

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.path = self.dir / "audit-log.jsonl"
        self.bundles = self.dir / "bundles"

    def _lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return [line for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def entries(self) -> list[dict]:
        return [json.loads(line) for line in self._lines()]

    def append(self, record: dict, bundle: bytes) -> dict:
        with _WRITE_LOCK:
            self.bundles.mkdir(parents=True, exist_ok=True)
            bundle_file = self.bundles / f"{record['id']}.zip"
            bundle_file.write_bytes(bundle)
            lines = self._lines()
            previous = json.loads(lines[-1])["entry_hash"] if lines else GENESIS
            entry = {"record": record,
                     "bundle": {"file": f"bundles/{bundle_file.name}", "sha256": sha256_bytes(bundle)},
                     "prev_hash": previous}
            entry["entry_hash"] = _entry_hash(entry)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            return entry

    def verify(self) -> tuple[bool, int, list[str]]:
        """(intact, number of entries, problems found)."""
        problems: list[str] = []
        previous = GENESIS
        lines = self._lines()
        for number, line in enumerate(lines, 1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"Entry {number} is not valid JSON.")
                previous = ""
                continue
            if entry.get("prev_hash") != previous:
                problems.append(f"Entry {number} does not follow the entry before it: history was removed or reordered.")
            if _entry_hash(entry) != entry.get("entry_hash"):
                problems.append(f"Entry {number} was modified after it was written.")
            bundle = entry.get("bundle", {})
            bundle_path = self.dir / bundle.get("file", "")
            if not bundle_path.is_file():
                problems.append(f"Entry {number}: the exported bundle {bundle.get('file')} is missing.")
            elif sha256_bytes(bundle_path.read_bytes()) != bundle.get("sha256"):
                problems.append(f"Entry {number}: the exported bundle {bundle.get('file')} was modified.")
            previous = entry.get("entry_hash", "")
        return not problems, len(lines), problems
