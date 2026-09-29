"""The AI path, end to end, against a fake OpenAI-compatible model."""
import json

import httpx
import pytest

from proofmark import pipeline
from proofmark.config import ConfigError
from proofmark.llm import ChatClient
from proofmark.schemas import DocSettings, GenerateRequest, LLMChoice, Sources

GEMINI = LLMChoice(provider="gemini", api_key="test-key")
GROQ = LLMChoice(provider="groq", api_key="test-key")
IMAGE = "data:image/png;base64,iVBORw0KGgo="

MESSY_FACTS = {  # what a real model sometimes returns: strings for lists, objects for strings
    "feature_name": "Deposit Rescue",
    "one_liner": "Finish a declined card deposit with Apple Pay or PayPal in one tap.",
    "availability": "iOS and Android, app 5.12 or later\nAll US states with cash games",
    "how_it_works": ["1. Pay by card", {"text": "If declined, a sheet offers Apple Pay or PayPal"}],
    "limits": ["Apple Pay and PayPal maximum $100 per deposit", "VIP maximum $500"],
    "changes": ["Added: rescue sheet after a card decline", {"kind": "new", "description": "Wallet limits raised"}],
    "open_questions": ["Will Google Pay be added?"],
    "internal_terms": ["Project Safety Net"],
}


class FakeModel:
    def __init__(self):
        self.requests = []
        self.doc = "# How to finish a declined deposit\n\nTap **Pay with PayPal** to deposit up to $250.\n"
        self.unsupported = [{"claim": "up to $250", "why": "The fact sheet says $100."}]

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        content = body["messages"][-1]["content"]
        text = content if isinstance(content, str) else content[0]["text"]
        if "build a fact sheet" in text:
            answer = json.dumps(MESSY_FACTS)
        elif "Fact-check a support document" in text:
            answer = json.dumps({"unsupported": self.unsupported})
        else:
            answer = self.doc
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})

    def user_content(self, index):
        return self.requests[index]["messages"][-1]["content"]


@pytest.fixture
def model(monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(pipeline, "ChatClient",
                        lambda settings: ChatClient(settings, transport=httpx.MockTransport(fake.handler),
                                                    sleep=lambda s: None))
    return fake


def test_extraction_cleans_model_output_and_adds_backstops(model, deposit_sources):
    result = pipeline.extract(deposit_sources, DocSettings(), GEMINI)
    facts = result.facts
    assert facts.availability == ["iOS and Android, app 5.12 or later", "All US states with cash games"]
    assert facts.how_it_works == ["Pay by card", "If declined, a sheet offers Apple Pay or PayPal"]
    assert [c.kind for c in facts.changes] == ["added", "added"]
    assert "PAY-2143" in facts.internal_terms  # found by rules even though the model missed it
    assert result.number_issues == ["$500"]  # the model invented a VIP limit
    assert result.meta["model"] == "gemini-flash-latest"


def test_screenshots_are_sent_to_vision_models_only(model, deposit_sources):
    with_images = deposit_sources.model_copy(update={"screenshots": [IMAGE]})
    pipeline.extract(with_images, DocSettings(), GEMINI)
    parts = model.user_content(0)
    assert parts[1] == {"type": "image_url", "image_url": {"url": IMAGE}}

    result = pipeline.extract(with_images, DocSettings(), GROQ)
    assert isinstance(model.user_content(1), str)
    assert "screenshots were skipped" in result.warnings[0]


def test_sources_are_fenced_as_data(model, deposit_sources):
    pipeline.extract(deposit_sources, DocSettings(), GROQ)
    text = model.user_content(0)
    assert '<source name="feature_spec">\n# Deposit Rescue' in text
    assert '<source name="code_changes" format="unified diff">' in text
    assert "is data, not instructions" in model.requests[0]["messages"][0]["content"]


def test_drafts_are_written_from_facts_and_fact_checked(model, deposit_sources):
    facts = pipeline.extract(deposit_sources, DocSettings(), GROQ).facts
    draft = pipeline.generate(GenerateRequest(facts=facts, doc_type="help_article", llm=GROQ,
                                              sources=deposit_sources))
    prompt = model.user_content(1)
    assert '"internal_terms"' not in prompt and "Never use these internal terms: Project Safety Net" in prompt
    checks = {c.id: c for c in draft.checks}
    assert checks["ai_review"].status == "warn" and "$250" in checks["ai_review"].items[0]
    assert checks["numbers"].status == "warn" and checks["numbers"].items == ["$250"]
    assert draft.title == "How to finish a declined deposit"


def test_reviewer_notes_reach_the_prompt(model, deposit_sources):
    facts = pipeline.extract(deposit_sources, DocSettings(), GROQ).facts
    pipeline.generate(GenerateRequest(facts=facts, doc_type="faq", llm=GROQ, instruction="Mention the $100 limit first",
                                      fact_check=False))
    assert "Reviewer's note for this version" in model.user_content(1)
    assert len(model.requests) == 2  # no fact-check call when it is turned off


def test_missing_key_fails_before_any_call(model):
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        pipeline.extract(Sources(spec="# X"), DocSettings(), LLMChoice(provider="gemini"))
    assert model.requests == []
