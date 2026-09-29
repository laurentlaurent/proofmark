import pytest

from proofmark import pipeline
from proofmark.schemas import DOC_TYPES, Clarification, DocSettings, GenerateRequest, LLMChoice, Sources

DEMO = LLMChoice(provider="demo")


def test_extracts_facts_from_spec_diff_and_previous_version(deposit_sources):
    result = pipeline.extract(deposit_sources, DocSettings(), DEMO)
    facts = result.facts
    assert facts.feature_name == "Deposit Rescue"
    assert len(facts.how_it_works) == 5
    assert any("$100" in item for item in facts.limits)
    assert {"Project Safety Net", "PAY-2143", "deposit_rescue_enabled"} <= set(facts.internal_terms)
    assert any(c.kind == "removed" and "Payment failed" in c.description for c in facts.changes)
    assert any(c.kind == "changed" and "$15" in c.description for c in facts.changes)
    assert "We couldn't complete your deposit. No money was taken." in " ".join(facts.ui_text)
    assert result.number_issues == []
    assert result.version_diff.startswith("--- previous version")


def test_open_questions_combine_the_spec_and_the_gap_checklist(deposit_sources):
    questions = pipeline.extract(deposit_sources, DocSettings(), DEMO).facts.open_questions
    assert questions[0] == "Will Google Pay be added to the rescue sheet on Android?"
    assert any("turn this off" in q for q in questions)  # the spec has no settings section


def test_finds_help_centre_articles_that_go_stale(deposit_sources, streak_sources):
    deposit = [m.path for m in pipeline.extract(deposit_sources, DocSettings(), DEMO).kb_matches]
    assert {"knowledge_base/adding-funds.md", "knowledge_base/deposit-declined.md"} <= set(deposit[:3])
    adding_funds = next(m for m in pipeline.extract(deposit_sources, DocSettings(), DEMO).kb_matches
                        if m.path.endswith("adding-funds.md"))
    assert "$15" in adding_funds.excerpt  # shows the sentence that is now wrong
    streak = pipeline.extract(streak_sources, DocSettings(), DEMO).kb_matches
    assert streak[0].path == "knowledge_base/daily-streaks.md"


@pytest.mark.parametrize("doc_type", list(DOC_TYPES))
def test_every_document_type_drafts_and_is_checked(deposit_sources, doc_type):
    extracted = pipeline.extract(deposit_sources, DocSettings(), DEMO)
    draft = pipeline.generate(GenerateRequest(facts=extracted.facts, doc_type=doc_type, llm=DEMO,
                                              sources=deposit_sources, kb_matches=extracted.kb_matches))
    assert draft.markdown.startswith("# ")
    assert draft.html.startswith("<h1>")
    ids = [c.id for c in draft.checks]
    assert ids == ["ai_review", "numbers", "jargon", "placeholders", "reading_level", "structure", "open_questions"]
    by_id = {c.id: c for c in draft.checks}
    assert by_id["numbers"].status == "pass"
    assert by_id["placeholders"].status == "pass"
    assert by_id["jargon"].status != "fail"  # no code names leak into drafts


def test_answers_become_facts_in_drafts(deposit_sources):
    facts = pipeline.extract(deposit_sources, DocSettings(), DEMO).facts
    facts.clarifications.append(Clarification(question="Will Google Pay be added?", answer="Not at launch."))
    facts.open_questions = []
    draft = pipeline.generate(GenerateRequest(facts=facts, doc_type="faq", llm=DEMO))
    assert "### Will Google Pay be added?" in draft.markdown
    assert next(c for c in draft.checks if c.id == "open_questions").status == "pass"


def test_whats_changed_needs_a_previous_version(streak_sources):
    facts = pipeline.extract(streak_sources, DocSettings(), DEMO).facts
    with pytest.raises(pipeline.UserInputError):
        pipeline.generate(GenerateRequest(facts=facts, doc_type="whats_changed", llm=DEMO, sources=streak_sources))


def test_empty_spec_is_rejected_with_a_helpful_message():
    with pytest.raises(pipeline.UserInputError, match="Add a feature spec"):
        pipeline.extract(Sources(spec="   "), DocSettings(), DEMO)


def test_clean_markdown_strips_fences_and_chatter():
    raw = "Sure! Here is the article:\n\n```markdown\n# Title\n\nBody\n```"
    assert pipeline.clean_markdown(raw, "Fallback") == "# Title\n\nBody\n"
    assert pipeline.clean_markdown("```\n# Title\n\nBody\n```", "Fallback") == "# Title\n\nBody\n"
    assert pipeline.clean_markdown("No title here", "Fallback").startswith("# Fallback\n")
