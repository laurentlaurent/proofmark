import hashlib
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from proofmark import audit, config, pipeline
from proofmark.__main__ import main
from proofmark.schemas import Approval, DocSettings, GenerateRequest, LLMChoice
from proofmark.server import app

DEMO = LLMChoice(provider="demo")


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def drafted(deposit_sources):
    extracted = pipeline.extract(deposit_sources, DocSettings(), DEMO)
    drafts = {t: pipeline.generate(GenerateRequest(facts=extracted.facts, doc_type=t, llm=DEMO, sources=deposit_sources,
                                                   kb_matches=extracted.kb_matches))
              for t in ("help_article", "faq", "agent_brief")}
    return extracted, drafts


def test_record_captures_provenance_checks_and_approvals(drafted, deposit_sources):
    extracted, drafts = drafted
    article = drafts["help_article"]
    article.approval = Approval(by="Alex", at="2026-09-28T10:00:00Z", sha256=sha(article.markdown))
    record = audit.build_record(extracted.facts, list(drafts.values()), DocSettings(), deposit_sources, extracted.meta)
    assert record["schema"] == "proofmark.audit/1"
    assert record["sources"]["spec"]["sha256"] == sha(deposit_sources.spec)
    assert record["fact_sheet"]["extracted_by"]["provider"] == "demo"
    doc = record["documents"][0]
    assert doc["drafted_by"]["prompt_version"] and doc["edited_after_drafting"] is False
    assert doc["approval"] == {"by": "Alex", "at": "2026-09-28T10:00:00Z", "note": "", "valid": True, "problem": ""}
    assert record["summary"]["approved"] == 1 and record["summary"]["documents"] == 3


def test_approval_of_a_changed_draft_does_not_count(drafted, deposit_sources):
    extracted, drafts = drafted
    faq = drafts["faq"]
    faq.approval = Approval(by="Alex", sha256=sha(faq.markdown))
    faq.markdown += "\nOne more line added after approval.\n"
    record = audit.build_record(extracted.facts, [faq], DocSettings(), deposit_sources, extracted.meta)
    doc = record["documents"][0]
    assert doc["approval"]["valid"] is False and doc["edited_after_drafting"] is True
    assert record["summary"] == {**record["summary"], "approved": 0, "invalid_approvals": 1}
    ai = next(c for c in doc["checks"] if c["id"] == "ai_review")
    assert "Edited after drafting" in ai["detail"]


def test_approving_failing_checks_requires_a_reason(drafted, deposit_sources):
    extracted, drafts = drafted
    article = drafts["help_article"]
    article.markdown = article.markdown.replace("Deposit Rescue", "Project Safety Net", 1)  # a code-name leak
    article.approval = Approval(by="Alex", sha256=sha(article.markdown))
    with pytest.raises(pipeline.UserInputError, match="without a note"):
        audit.build_record(extracted.facts, [article], DocSettings(), deposit_sources, extracted.meta)
    article.approval.note = "Legal asked to keep the project name in this article."
    record = audit.build_record(extracted.facts, [article], DocSettings(), deposit_sources, extracted.meta)
    assert record["summary"]["approved_with_failures"] == 1


def test_the_log_detects_tampering(tmp_path):
    log = audit.AuditLog(tmp_path / "audit")
    for number in range(3):
        log.append({"id": f"r{number}", "summary": {}, "documents": []}, f"bundle {number}".encode())
    assert log.verify() == (True, 3, [])

    lines = log.path.read_text(encoding="utf-8").splitlines()
    edited = json.loads(lines[1])
    edited["record"]["id"] = "rewritten"
    log.path.write_text("\n".join([lines[0], json.dumps(edited), lines[2]]) + "\n")
    intact, _, problems = log.verify()
    assert not intact and "Entry 2 was modified" in problems[0]

    log.path.write_text("\n".join(lines[1:]) + "\n")  # the first entry removed
    assert "does not follow" in log.verify()[2][0]

    log.path.write_text("\n".join(lines) + "\n")
    (tmp_path / "audit" / "bundles" / "r0.zip").write_bytes(b"swapped")
    assert "bundle bundles/r0.zip was modified" in log.verify()[2][0]


def test_export_records_the_approval_and_bundles_the_evidence(drafted, deposit_sources):
    extracted, drafts = drafted
    article = drafts["help_article"]
    article.approval = Approval(by="Alex", at="2026-09-28T10:00:00Z", sha256=sha(article.markdown))
    sources = deposit_sources.model_copy(update={"screenshots": [
        "data:image/png;base64," + __import__("base64").b64encode(b"\x89PNG fake").decode()]})
    response = TestClient(app).post("/api/export", json={
        "facts": extracted.facts.model_dump(), "drafts": [d.model_dump() for d in drafts.values()],
        "sources": sources.model_dump(), "extract_meta": extracted.meta})
    assert response.status_code == 200
    assert response.headers["x-proofmark-approved"] == "1/3"
    assert response.headers["x-proofmark-audit-log"] == "saved"

    bundle = zipfile.ZipFile(io.BytesIO(response.content))
    names = set(bundle.namelist())
    assert {"deposit-rescue/audit.json", "deposit-rescue/sources/spec.md", "deposit-rescue/sources/previous-version.md",
            "deposit-rescue/sources/changes.diff", "deposit-rescue/sources/screenshot-1.png"} <= names
    record = json.loads(bundle.read("deposit-rescue/audit.json"))
    assert record["id"] == response.headers["x-proofmark-audit-id"]
    assert "approved by Alex" in bundle.read("deposit-rescue/check-report.md").decode()

    log = audit.AuditLog(config.AUDIT_DIR)
    entry = log.entries()[-1]
    assert entry["record"]["id"] == record["id"]
    assert entry["bundle"]["sha256"] == hashlib.sha256(response.content).hexdigest()
    assert main(["audit", "--verify", "--dir", str(config.AUDIT_DIR)]) == 0


def test_export_can_be_kept_out_of_the_log(drafted, monkeypatch):
    extracted, drafts = drafted
    monkeypatch.setattr(config, "AUDIT_ENABLED", False)
    response = TestClient(app).post("/api/export", json={
        "facts": extracted.facts.model_dump(), "drafts": [drafts["faq"].model_dump()]})
    assert response.status_code == 200 and "x-proofmark-audit-log" not in response.headers
    assert not (config.AUDIT_DIR / "audit-log.jsonl").exists()
