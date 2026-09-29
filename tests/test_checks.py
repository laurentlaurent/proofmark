from proofmark import checks
from proofmark.export import md_to_html
from proofmark.schemas import DocSettings, FactSheet, Sources

SOURCE = "Deposit up to $100. Pending charges clear within 1 to 3 business days. App 5.12. Every seven days."


def test_numbers_not_in_the_sources_are_flagged():
    doc = "1. Deposit up to $250.\n2. Funds arrive in 30 seconds.\nIt clears in 3 business days on app 5.12."
    assert checks.ungrounded_numbers(doc, SOURCE) == ["$250", "30 seconds"]


def test_numbers_in_the_sources_pass_in_any_format():
    doc = "You can deposit up to $100.00, every 7 days, on version 5.12."
    assert checks.ungrounded_numbers(doc, SOURCE) == []


def test_list_markers_and_plain_single_digits_are_ignored():
    assert checks.ungrounded_numbers("1. Tap Deposit.\n2. Pick 1 of 2 options.", SOURCE) == []


def test_internal_terms_fail_in_player_docs_but_not_in_agent_briefs():
    facts = FactSheet(feature_name="X", internal_terms=["Project Safety Net", "PAY-2143"])
    md = "# Title\n\nProject Safety Net helps you. See the backend."
    player = checks.check_jargon(md, "help_article", facts)
    assert player.status == "fail" and player.items == ["Project Safety Net", "backend"]
    assert checks.check_jargon(md, "agent_brief", facts).status == "info"
    assert checks.check_jargon("# Title\n\nTap Deposit.", "faq", facts).status == "pass"


def test_placeholders_are_caught_without_flagging_normal_words():
    assert checks.check_placeholders("# T\n\nAvailable in [TBD].").status == "fail"
    assert checks.check_placeholders("# T\n\nTODO: add link").status == "fail"
    assert checks.check_placeholders("# T\n\nPuedes usar todo el saldo.").status == "pass"


def test_reading_level_separates_plain_from_dense_text():
    plain = "# Add funds\n\nTap your balance. Tap Deposit. Pick an amount. Confirm with Face ID."
    dense = ("# Remediation\n\nSubsequent authorisation irregularities necessitate comprehensive "
             "reconciliation procedures, notwithstanding intermediary institutional considerations.")
    settings = DocSettings()
    assert checks.check_reading_level(plain, "help_article", settings).status == "pass"
    assert checks.check_reading_level(dense, "help_article", settings).status == "fail"
    assert checks.check_reading_level(dense, "help_article", DocSettings(language="French")).status == "info"


def test_structure_rules_per_document_type():
    settings = DocSettings()
    faq = "# FAQ\n\n### What is it?\nA thing.\n\n### Who gets it?\nEveryone."
    assert checks.check_structure(faq, "faq", settings).status == "warn"
    long_blurb = "# What's new\n\n" + " ".join(["word"] * 60) + "\n\n## Details\n- New: x"
    assert "Blurb is 60 words" in checks.check_structure(long_blurb, "release_notes", settings).items[0]
    article = "# How to add funds\n\nIntro.\n\n## Steps\n\n1. Tap Deposit.\n2. Confirm."
    assert checks.check_structure(article, "help_article", settings).status == "pass"


def test_numbers_check_accepts_facts_the_user_added():
    facts = FactSheet(feature_name="X", clarifications=[{"question": "Max?", "answer": "$500 for VIPs"}])
    check = checks.check_numbers("# X\n\nVIPs can deposit $500.", facts, Sources(spec="nothing"))
    assert check.status == "pass"


def test_rendered_html_is_sanitised():
    html = md_to_html("# Hi\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1)) "
                      "[good](https://blitz.example)\n\n<img src=x onerror=alert(1)>")
    assert "<script" not in html and "alert(1)</" not in html
    assert "javascript:" not in html and "<img" not in html and "onerror" not in html
    assert '<a href="https://blitz.example" rel="noopener noreferrer" target="_blank">good</a>' in html
