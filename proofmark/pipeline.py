"""The pipeline: sources -> fact sheet -> drafts -> checks.

The same functions back the web API, the CLI and the tests.
"""
from __future__ import annotations

import difflib
import re
import time

from . import checks, config, demo, kb, prompts
from .audit import now_iso, sha256_text
from .errors import UserInputError
from .export import md_to_html
from .llm import ChatClient, LLMError
from .schemas import (CheckResponse, DocSettings, Draft, ExtractResponse, FactSheet, GenerateRequest,
                      KBMatch, LLMChoice, Sources)

__all__ = ["UserInputError", "extract", "generate", "recheck", "version_diff"]


def _llm(choice: LLMChoice | None) -> config.LLMSettings:
    choice = choice or LLMChoice()
    llm = config.resolve(choice.provider, choice.model, choice.api_key, choice.base_url, choice.vision)
    llm.ensure_ready()
    return llm


def version_diff(previous: str, current: str) -> str:
    if not previous.strip():
        return ""
    lines = difflib.unified_diff(previous.strip().splitlines(), current.strip().splitlines(),
                                 fromfile="previous version", tofile="this version", lineterm="", n=1)
    return "\n".join(lines)


def merge_terms(existing: list[str], extra: list[str]) -> list[str]:
    seen = {t.lower() for t in existing}
    merged = list(existing)
    for term in extra:
        if term.lower() not in seen:
            merged.append(term)
            seen.add(term.lower())
    return merged


def extract(sources: Sources, settings: DocSettings, choice: LLMChoice | None = None) -> ExtractResponse:
    if not sources.spec.strip():
        raise UserInputError("Add a feature spec first: paste it, upload a file, or load a sample.")
    llm = _llm(choice)
    started = time.perf_counter()
    warnings: list[str] = []
    meta: dict = {"provider": llm.provider, "model": llm.model, "prompt_version": prompts.PROMPT_VERSION,
                  "extracted_at": now_iso()}

    if llm.is_demo:
        facts = demo.extract_facts(sources)
        meta["images_used"] = 0
        if sources.screenshots:
            warnings.append("Demo mode does not read screenshots. Pick an AI model with vision to use them.")
    else:
        use_images = bool(sources.screenshots) and llm.vision
        if sources.screenshots and not llm.vision:
            warnings.append(f"{llm.model} does not read images, so the screenshots were skipped. "
                            "Gemini, or a vision model in Ollama, can use them.")
        with ChatClient(llm) as client:
            data = client.chat_json(prompts.extraction_messages(sources, settings, use_images), temperature=0.1)
            meta.update(calls=client.calls, usage=client.usage)
        facts = FactSheet.model_validate(data)
        meta["images_used"] = len(sources.screenshots) if use_images else 0

    # Deterministic backstops, whatever the model did.
    facts.internal_terms = merge_terms(facts.internal_terms, demo.detect_internal_terms(sources))
    number_issues = checks.ungrounded_numbers(facts.as_text(), sources.as_text())
    matches = kb.rank_articles(facts, kb.load_articles(config.KB_DIR))
    meta["seconds"] = round(time.perf_counter() - started, 1)
    return ExtractResponse(facts=facts, warnings=warnings, number_issues=number_issues, kb_matches=matches,
                           version_diff=version_diff(sources.previous_spec, sources.spec), meta=meta)


def clean_markdown(text: str, fallback_title: str) -> str:
    """Remove what models wrap around a document: chatter, code fences."""
    text = (text or "").strip()
    if not text.startswith("# "):
        fenced = re.search(r"```(?:markdown|md)?[ \t]*\n(.*?)\n```", text, re.S | re.I)
        if fenced and re.search(r"^# ", fenced.group(1), re.M):
            text = fenced.group(1).strip()
    lines = text.splitlines()
    first_title = next((i for i, line in enumerate(lines) if line.startswith("# ")), None)
    if first_title is None:
        lines = [f"# {fallback_title}", ""] + lines
    elif first_title > 0:
        lines = lines[first_title:]  # drop chit-chat before the title
    return "\n".join(lines).strip() + "\n"


def title_of(markdown_text: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+)$", markdown_text or "", re.M)
    return match.group(1).strip() if match else fallback


def _kb_context(matches: list[KBMatch]) -> str:
    if not matches:
        return "No existing article matched."
    return "\n".join(f"- {m.title} ({m.path}). Matching text: {m.excerpt or ', '.join(m.terms)}" for m in matches)


def generate(req: GenerateRequest) -> Draft:
    facts, doc_type = req.facts, req.doc_type
    has_previous = bool(req.sources and req.sources.previous_spec.strip())
    if doc_type == "whats_changed" and not (facts.changes or has_previous):
        raise UserInputError("'What changed' needs a previous version of the spec, or changes in the fact sheet.")
    llm = _llm(req.llm)
    started = time.perf_counter()
    meta: dict = {"provider": llm.provider, "model": llm.model, "prompt_version": prompts.PROMPT_VERSION}
    unsupported: list[dict] | None = None
    review_note = ""

    if llm.is_demo:
        markdown_text = demo.render_doc(doc_type, facts, req.settings, req.kb_matches)
        review_note = "Needs an AI model. Demo drafts only reuse the fact sheet."
    else:
        extra: dict[str, str] = {}
        if doc_type == "whats_changed":
            if has_previous:
                extra["spec_diff"] = version_diff(req.sources.previous_spec, req.sources.spec)
            extra["existing_articles"] = _kb_context(req.kb_matches)
        with ChatClient(llm) as client:
            raw = client.chat(prompts.doc_messages(doc_type, facts, req.settings, extra, req.instruction),
                              temperature=0.4)
            markdown_text = clean_markdown(raw, facts.feature_name)
            if req.fact_check:
                try:
                    review = client.chat_json(prompts.review_messages(facts, markdown_text, req.settings),
                                              temperature=0.0)
                    found = review.get("unsupported", [])
                    unsupported = found if isinstance(found, list) else []
                except LLMError as exc:
                    review_note = f"Could not run: {exc}"
            else:
                review_note = "Turned off for this draft."
            meta.update(calls=client.calls, usage=client.usage)

    found_checks = checks.run_all(markdown_text, doc_type, facts, req.sources, req.settings, unsupported, review_note)
    meta["seconds"] = round(time.perf_counter() - started, 1)
    meta["drafted_at"] = now_iso()
    meta["generated_sha256"] = sha256_text(markdown_text)  # lets the audit tell hand edits apart
    if req.instruction.strip():
        meta["instruction"] = req.instruction.strip()
    return Draft(doc_type=doc_type, title=title_of(markdown_text, facts.feature_name), markdown=markdown_text,
                 html=md_to_html(markdown_text), checks=found_checks, meta=meta)


def recheck(doc_type: str, markdown_text: str, facts: FactSheet, sources: Sources | None,
            settings: DocSettings) -> CheckResponse:
    """Re-run the deterministic checks after a human edit (no model call)."""
    found = checks.run_all(markdown_text, doc_type, facts, sources, settings, None,
                           "Edited by hand. Regenerate to fact-check again.")
    return CheckResponse(title=title_of(markdown_text, facts.feature_name), html=md_to_html(markdown_text),
                         checks=found)
