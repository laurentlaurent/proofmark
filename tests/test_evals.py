import json

import pytest

from proofmark import config, evals
from proofmark.__main__ import main
from proofmark.schemas import LLMChoice

DEMO = LLMChoice(provider="demo")


def test_matching_is_tolerant_but_exact_on_numbers():
    assert evals.contains("Deposit up to $100 today", "$100")
    assert not evals.contains("Deposit up to $1000 today", "$100")
    assert not evals.contains("Version 15.12", "5.12")
    assert evals.contains("Open Settings \u2192 Notifications", "Settings > Notifications")
    assert evals.contains("You can\u2019t buy Shields", ["cannot buy", "can't buy"])
    assert evals.contains("EVERY SEVEN DAYS", ["7 days", "seven days"])


def test_demo_mode_meets_its_saved_baseline():
    report = evals.run(DEMO, progress=lambda _: None)
    metrics = report["metrics"]
    assert metrics["leaks"] == 0 and metrics["errors"] == 0
    assert metrics["fact_sheet_coverage"] == 1.0 and metrics["open_question_recall"] == 1.0
    baseline = json.loads(evals.baseline_path("demo", "rules").read_text(encoding="utf-8"))
    assert not [row for row in evals.compare(metrics, baseline) if row["regressed"]]


def test_regressions_respect_direction_and_tolerance():
    baseline = {"metrics": {"doc_fact_coverage": 0.90, "fact_sheet_coverage": 0.90, "unsupported_claims": 2,
                            "avg_reading_grade": 6.0}}
    now = {"doc_fact_coverage": 0.86, "fact_sheet_coverage": 0.80, "unsupported_claims": 4, "avg_reading_grade": 6.9,
           "leaks": 0, "errors": 0}
    regressed = {row["metric"] for row in evals.compare(now, baseline) if row["regressed"]}
    assert regressed == {"fact_sheet_coverage", "unsupported_claims"}  # the other two are within tolerance


def test_leaks_and_errors_fail_even_without_a_baseline():
    rows = evals.compare({"leaks": 1, "errors": 0}, None)
    assert [row["metric"] for row in rows if row["regressed"]] == ["leaks"]


def test_forbidden_text_is_counted_as_a_leak(tmp_path):
    case = {"id": "leaky", "sources": {"spec": "samples/deposit-rescue/spec.md"},
            "expect": {"documents": {"help_article": {"must_not_include": ["Apple Pay"]}}}}
    (tmp_path / "leaky.json").write_text(json.dumps(case))
    report = evals.run(DEMO, cases_dir=tmp_path, progress=lambda _: None)
    assert report["metrics"]["leaks"] == 1
    assert "help_article: contains forbidden \u201cApple Pay\u201d" in report["cases"][0]["findings"]


def test_cli_exits_with_1_on_a_regression(tmp_path, monkeypatch):
    (tmp_path / "cases").mkdir()
    case = {"id": "gaps", "sources": {"spec": "samples/streak-shield/spec.md"},
            "expect": {"open_questions": ["refund"], "documents": {}}}  # a gap the demo will not raise
    (tmp_path / "cases" / "gaps.json").write_text(json.dumps(case))
    monkeypatch.setattr(config, "EVALS_DIR", tmp_path)
    baseline = evals.baseline_path("demo", "rules")
    baseline.parent.mkdir()
    baseline.write_text(json.dumps({"metrics": {"open_question_recall": 1.0}}))
    out = tmp_path / "out"
    assert main(["eval", "--provider", "demo", "--out", str(out)]) == 1
    assert "Regression: worse than the baseline" in (out / "eval-report.md").read_text(encoding="utf-8")
    assert main(["eval", "--provider", "demo", "--out", str(out), "--save-baseline"]) == 0
    assert main(["eval", "--provider", "demo", "--out", str(out)]) == 0
