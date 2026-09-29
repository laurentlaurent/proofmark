"""Regression evaluation: run golden cases through the pipeline and score them.

Each case in evals/cases/*.json names its sources and what a good result must,
and must not, contain. Scores are compared with a saved baseline for the same
provider and model, so a prompt or model change that makes Proofmark worse
fails loudly (exit code 1), locally or in CI.

    python -m proofmark eval                    # score, compare with the baseline
    python -m proofmark eval --save-baseline    # accept the current scores

An expectation is a string or a list of alternatives, any of which counts,
because models paraphrase ("7 days" or "seven days"). Matching ignores case,
typographic quotes and arrow styles, and "$100" never matches "$1000".
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import checks, config, pipeline, prompts
from .audit import now_iso
from .export import slugify
from .schemas import PLAYER_DOCS, DocSettings, GenerateRequest, LLMChoice, Sources

# name: (label, higher is better, tolerated change before it counts as a regression)
METRICS: dict[str, tuple[str, bool, float]] = {
    "fact_sheet_coverage": ("Key facts in the fact sheet", True, 0.05),
    "internal_terms_recall": ("Internal terms detected", True, 0.0),
    "open_question_recall": ("Known gaps raised as questions", True, 0.10),
    "stale_article_recall": ("Stale articles found", True, 0.0),
    "doc_fact_coverage": ("Key facts in the documents", True, 0.05),
    "leaks": ("Forbidden text in documents", False, 0),
    "docs_with_failing_checks": ("Documents with failing checks", False, 0),
    "unsupported_claims": ("Claims flagged by the AI fact-check", False, 1),
    "avg_reading_grade": ("Average reading grade, player docs", False, 1.0),
    "errors": ("Cases that could not run", False, 0),
}
RATIOS = {"fact_sheet_coverage", "internal_terms_recall", "open_question_recall", "stale_article_recall",
          "doc_fact_coverage"}
ABSOLUTE_LIMITS = {"leaks": 0, "errors": 0}  # must hold whatever the baseline says


def normalise(text: str) -> str:
    text = (text or "").lower()
    text = text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    text = re.sub(r"\s*(?:>|\u2192|\u203a|->)\s*", " > ", text)
    return re.sub(r"\s+", " ", text).strip()


def contains(text: str, expected: str | list[str]) -> bool:
    """True if the text contains the expectation, or any of its alternatives."""
    options = expected if isinstance(expected, list) else [expected]
    haystack = normalise(text)
    for option in options:
        needle = normalise(option)
        if not needle:
            continue
        pattern = re.escape(needle)
        if needle[0].isdigit():
            pattern = r"(?<![\d.,])" + pattern
        if needle[-1].isdigit():
            pattern += r"(?![\d])"
        if re.search(pattern, haystack):
            return True
    return False


def label_of(expected: str | list[str]) -> str:
    return expected if isinstance(expected, str) else " / ".join(expected)


def load_cases(cases_dir: Path, only: list[str] | None = None) -> list[dict]:
    cases = []
    for path in sorted(Path(cases_dir).glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        case.setdefault("id", path.stem)
        if not only or case["id"] in only:
            cases.append(case)
    return cases


def _read(root: Path, relative: str | None) -> str:
    return (root / relative).read_text(encoding="utf-8") if relative else ""


def run_case(case: dict, choice: LLMChoice, fact_check: bool, root: Path) -> dict:
    """Score one case. Counts are kept raw so they can be summed across cases."""
    src = case["sources"]
    sources = Sources(spec=_read(root, src.get("spec")), previous_spec=_read(root, src.get("previous_spec")),
                      code_diff=_read(root, src.get("code_diff")))
    settings = DocSettings(**case.get("settings", {}))
    expect = case.get("expect", {})
    extracted = pipeline.extract(sources, settings, choice)
    facts = extracted.facts
    counts = {name: [0, 0] for name in RATIOS}  # [found, expected]
    findings: list[str] = []

    def score(metric: str, text: str, expectations: list, where: str) -> None:
        for expected in expectations:
            counts[metric][1] += 1
            if contains(text, expected):
                counts[metric][0] += 1
            else:
                findings.append(f"{where}: missing \u201c{label_of(expected)}\u201d")

    score("fact_sheet_coverage", facts.as_text(), expect.get("facts", []), "fact sheet")
    score("internal_terms_recall", "\n".join(facts.internal_terms), expect.get("internal_terms", []), "internal terms")
    score("open_question_recall", "\n".join(facts.open_questions), expect.get("open_questions", []), "open questions")
    score("stale_article_recall", "\n".join(m.path for m in extracted.kb_matches),
          expect.get("stale_articles", []), "stale articles")

    leaks = failing = unsupported = 0
    grades: list[float] = []
    documents = {}
    for doc_type, doc_expect in expect.get("documents", {}).items():
        draft = pipeline.generate(GenerateRequest(facts=facts, doc_type=doc_type, settings=settings, llm=choice,
                                                  sources=sources, kb_matches=extracted.kb_matches,
                                                  fact_check=fact_check))
        score("doc_fact_coverage", draft.markdown, doc_expect.get("must_include", []), doc_type)
        forbidden = list(doc_expect.get("must_not_include", []))
        if doc_type in PLAYER_DOCS:
            forbidden += expect.get("player_docs_must_not_include", [])
        for item in forbidden:
            if contains(draft.markdown, item):
                leaks += 1
                findings.append(f"{doc_type}: contains forbidden \u201c{label_of(item)}\u201d")
        failed = [c.label for c in draft.checks if c.status == "fail"]
        if failed:
            failing += 1
            findings.append(f"{doc_type}: failing checks ({', '.join(failed)})")
        ai_review = next((c for c in draft.checks if c.id == "ai_review"), None)
        if ai_review is not None and ai_review.status == "warn":
            unsupported += len(ai_review.items)
        if doc_type in PLAYER_DOCS and settings.is_english:
            stats = checks.readability(draft.markdown)
            if stats:
                grades.append(stats["grade"])
        documents[doc_type] = {"title": draft.title, "status": [c.status for c in draft.checks]}
    return {"id": case["id"], "counts": counts, "leaks": leaks, "docs_with_failing_checks": failing,
            "unsupported_claims": unsupported, "grades": grades, "findings": findings, "documents": documents}


def aggregate(results: list[dict], errors: int) -> dict:
    metrics: dict[str, float | None] = {}
    for name in RATIOS:
        found = sum(r["counts"][name][0] for r in results)
        expected = sum(r["counts"][name][1] for r in results)
        metrics[name] = round(found / expected, 3) if expected else None
    for name in ("leaks", "docs_with_failing_checks", "unsupported_claims"):
        metrics[name] = sum(r[name] for r in results)
    grades = [g for r in results for g in r["grades"]]
    metrics["avg_reading_grade"] = round(sum(grades) / len(grades), 1) if grades else None
    metrics["errors"] = errors
    return {name: metrics.get(name) for name in METRICS}


def run(choice: LLMChoice, cases_dir: Path | None = None, only: list[str] | None = None, fact_check: bool = True,
        progress=print) -> dict:
    cases_dir = Path(cases_dir or config.EVALS_DIR / "cases")
    llm = config.resolve(choice.provider, choice.model, choice.api_key, choice.base_url, choice.vision)
    llm.ensure_ready()
    cases = load_cases(cases_dir, only)
    if not cases:
        raise pipeline.UserInputError(f"No evaluation cases found in {cases_dir}.")
    root = config.ROOT_DIR
    results, failures = [], []
    for case in cases:
        started = time.perf_counter()
        try:
            result = run_case(case, choice, fact_check, root)
            result["seconds"] = round(time.perf_counter() - started, 1)
            results.append(result)
            progress(f"  {case['id']:<28} scored in {result['seconds']} s")
        except Exception as exc:  # a failed case is recorded, not fatal: the rest still runs
            failures.append({"id": case["id"], "error": f"{exc.__class__.__name__}: {exc}"})
            progress(f"  {case['id']:<28} could not run: {exc}")
    return {"run_at": now_iso(), "provider": llm.provider, "model": llm.model,
            "prompt_version": prompts.PROMPT_VERSION, "fact_check": fact_check, "cases": results,
            "failed_cases": failures, "metrics": aggregate(results, len(failures))}


def baseline_path(provider: str, model: str, evals_dir: Path | None = None) -> Path:
    return Path(evals_dir or config.EVALS_DIR) / "baselines" / f"{provider}--{slugify(model)}.json"


def save_baseline(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = {key: report[key] for key in ("run_at", "provider", "model", "prompt_version", "fact_check", "metrics")}
    path.write_text(json.dumps(keep, indent=2) + "\n", encoding="utf-8")


def compare(metrics: dict, baseline: dict | None) -> list[dict]:
    """One row per metric: its value, the baseline, and whether it regressed."""
    rows = []
    base_metrics = (baseline or {}).get("metrics", {})
    for name, (label, higher_is_better, tolerance) in METRICS.items():
        now, base = metrics.get(name), base_metrics.get(name)
        regressed, reason = False, ""
        if now is not None and base is not None:
            change = now - base
            regressed = change < -tolerance if higher_is_better else change > tolerance
            if regressed:
                reason = "worse than the baseline"
        limit = ABSOLUTE_LIMITS.get(name)
        if limit is not None and now is not None and now > limit:
            regressed, reason = True, f"must be {limit}"
        rows.append({"metric": name, "label": label, "now": now, "baseline": base, "regressed": regressed,
                     "reason": reason})
    return rows


def _fmt(name: str, value) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.0f}%" if name in RATIOS else f"{value:g}"


def render_report(report: dict, rows: list[dict], baseline_file: Path | None) -> str:
    lines = ["# Proofmark evaluation", "",
             f"Run {report['run_at']} with {report['provider']} ({report['model']}), prompt version "
             f"{report['prompt_version']}, AI fact-check {'on' if report['fact_check'] else 'off'}.", ""]
    if baseline_file:
        lines += [f"Compared with `{baseline_file.name}`.", ""]
    lines += ["| Metric | Now | Baseline | Result |", "| --- | --- | --- | --- |"]
    for row in rows:
        result = f"Regression: {row['reason']}" if row["regressed"] else "OK"
        lines.append(f"| {row['label']} | {_fmt(row['metric'], row['now'])} | "
                     f"{_fmt(row['metric'], row['baseline'])} | {result} |")
    lines += ["", "## Findings by case", ""]
    for case in report["cases"]:
        lines.append(f"### {case['id']}")
        if case["findings"]:
            lines.extend(f"- {finding}" for finding in case["findings"])
        else:
            lines.append("- Nothing missing, nothing forbidden.")
        lines.append("")
    for failed in report["failed_cases"]:
        lines += [f"### {failed['id']}", f"- Could not run: {failed['error']}", ""]
    return "\n".join(lines)
