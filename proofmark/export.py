"""Rendering and export.

Model output is untrusted, so rendered HTML goes through an allowlist
sanitizer before it reaches the browser. The export bundle holds everything a
reviewer needs: Markdown, standalone HTML, the fact sheet, the check report and
Zendesk-style article payloads.
"""
from __future__ import annotations

import html
import io
import json
import re
import zipfile
from html.entities import name2codepoint
from html.parser import HTMLParser

import markdown as md_lib

from .audit import decode_image
from .schemas import PLAYER_DOCS, DocSettings, Draft, FactSheet, Sources

ALLOWED_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "strong", "em", "b", "i", "code",
                "pre", "blockquote", "table", "thead", "tbody", "tr", "th", "td", "a", "hr", "br", "del"}
VOID_TAGS = {"hr", "br"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "template", "svg", "math"}

LOCALES = {"english": "en-us", "french": "fr", "spanish": "es", "german": "de", "italian": "it",
           "portuguese": "pt-br", "dutch": "nl"}


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in DROP_WITH_CONTENT:
            self.skip += 1
            return
        if self.skip or tag not in ALLOWED_TAGS:
            return
        extra = ""
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if re.match(r"^(https?:|mailto:|#)", href, re.I):
                extra = f' href="{html.escape(href, quote=True)}" rel="noopener noreferrer" target="_blank"'
        self.out.append(f"<{tag}{extra}>")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if tag in DROP_WITH_CONTENT:
            return
        if not self.skip and tag in ALLOWED_TAGS:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_WITH_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if not self.skip:
            self.out.append(f"&{name};" if name in name2codepoint else html.escape(f"&{name}"))

    def handle_charref(self, name: str) -> None:
        if not self.skip and re.fullmatch(r"x[0-9a-fA-F]+|\d+", name):
            self.out.append(f"&#{name};")


def sanitize_html(raw: str) -> str:
    parser = _Sanitizer()
    parser.feed(raw)
    parser.close()
    return "".join(parser.out)


def md_to_html(markdown_text: str) -> str:
    rendered = md_lib.markdown(markdown_text or "", extensions=["tables", "sane_lists", "fenced_code"])
    return sanitize_html(rendered)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:60] or "feature"


def locale_for(language: str) -> str:
    return LOCALES.get(language.strip().lower(), "en-us")


STANDALONE_CSS = """
body{font:17px/1.6 Georgia,'Times New Roman',serif;color:#172033;max-width:720px;margin:48px auto;padding:0 20px}
h1,h2,h3{font-family:system-ui,sans-serif;line-height:1.25}h1{font-size:1.9rem}h2{font-size:1.3rem;margin-top:2rem}
table{border-collapse:collapse;width:100%;font-size:.95rem}th,td{border:1px solid #d3d9e3;padding:6px 10px;text-align:left}
blockquote{margin:1rem 0;padding:.5rem 1rem;border-left:3px solid #2450b8;background:#f3f6fb}
"""


def standalone_html(title: str, body_html: str, language: str) -> str:
    lang = locale_for(language).split("-")[0]
    return (f'<!doctype html>\n<html lang="{lang}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{STANDALONE_CSS}</style></head>"
            f"<body>\n{body_html}\n</body></html>\n")


def zendesk_payloads(facts: FactSheet, drafts: list[Draft], settings: DocSettings) -> list[dict]:
    """Bodies for Zendesk's 'create article' endpoint. Section and permission IDs are account-specific."""
    labels = [slugify(facts.feature_name), "proofmark-draft"]
    return [
        {"article": {"title": draft.title, "body": draft.html or md_to_html(draft.markdown),
                     "locale": locale_for(settings.language), "draft": True, "label_names": labels}}
        for draft in drafts if draft.doc_type in PLAYER_DOCS
    ]


def _approval_line(entry: dict | None) -> str:
    approval = (entry or {}).get("approval")
    if not approval:
        return "- **Approval**: not approved."
    if not approval["valid"]:
        return f"- **Approval**: not valid. {approval['problem']}"
    note = f" Note: {approval['note']}" if approval["note"] else ""
    return f"- **Approval**: approved by {approval['by']} at {approval['at']}.{note}"


def check_report(drafts: list[Draft], record: dict | None = None) -> str:
    """Human-readable report. With an audit record, it shows the checks re-run at export."""
    lines = ["# Check report", ""]
    entries = {d["doc_type"]: d for d in (record or {}).get("documents", [])}
    for draft in drafts:
        entry = entries.get(draft.doc_type)
        lines.append(f"## {draft.title} ({draft.doc_type})")
        if record is not None:
            lines.append(_approval_line(entry))
        found = entry["checks"] if entry else [c.model_dump() for c in draft.checks]
        labels = {c.id: c.label for c in draft.checks}
        for check in found:
            label = labels.get(check["id"], check["id"])
            lines.append(f"- **{label}**: {check['status']}. {check['detail']}")
            lines.extend(f"    - {item}" for item in check["items"])
        lines.append("")
    return "\n".join(lines)


SOURCE_FILES = (("spec", "spec.md"), ("previous_spec", "previous-version.md"), ("code_diff", "changes.diff"))


def build_bundle(facts: FactSheet, drafts: list[Draft], settings: DocSettings, record: dict | None = None,
                 sources: Sources | None = None) -> bytes:
    folder = slugify(facts.feature_name)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(f"{folder}/facts.json", facts.model_dump_json(indent=2))
        for draft in drafts:
            body = md_to_html(draft.markdown)
            bundle.writestr(f"{folder}/{draft.doc_type}.md", draft.markdown)
            bundle.writestr(f"{folder}/html/{draft.doc_type}.html",
                            standalone_html(draft.title, body, settings.language))
        bundle.writestr(f"{folder}/check-report.md", check_report(drafts, record))
        bundle.writestr(f"{folder}/zendesk_articles.json",
                        json.dumps(zendesk_payloads(facts, drafts, settings), indent=2, ensure_ascii=False))
        if record is not None:
            bundle.writestr(f"{folder}/audit.json", json.dumps(record, indent=2, ensure_ascii=False))
        if sources is not None:
            for field, name in SOURCE_FILES:
                text = getattr(sources, field)
                if text.strip():
                    bundle.writestr(f"{folder}/sources/{name}", text)
            for number, url in enumerate(sources.screenshots, 1):
                decoded = decode_image(url)
                if decoded:
                    extension = decoded[0].split("/")[1].replace("jpeg", "jpg").split("+")[0]
                    bundle.writestr(f"{folder}/sources/screenshot-{number}.{extension}", decoded[1])
    return buffer.getvalue()
