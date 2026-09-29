"""Command line entry point.

    python -m proofmark                 # start the web app on http://127.0.0.1:8000
    python -m proofmark draft --spec samples/deposit-rescue/spec.md --out out/
    python -m proofmark eval            # regression evaluation against the saved baseline
    python -m proofmark audit --verify  # check the audit log has not been tampered with
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sys
import threading
import webbrowser
from pathlib import Path

from . import config

LEVELS = {"pass": 0, "info": 0, "warn": 1, "fail": 2}


def serve(host: str, port: int, open_browser: bool) -> None:
    import uvicorn

    url = f"http://{host}:{port}"
    print(f"Proofmark is running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run("proofmark.server:app", host=host, port=port, log_level="warning")


def _read(path: Path | None) -> str:
    return path.read_text(encoding="utf-8") if path else ""


def _image(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def draft(args: argparse.Namespace) -> int:
    from .export import check_report
    from .llm import LLMError
    from .pipeline import UserInputError, extract, generate
    from .schemas import DOC_TYPES, DocSettings, GenerateRequest, LLMChoice, Sources

    doc_types = [d.strip() for d in args.docs.split(",") if d.strip()]
    unknown = [d for d in doc_types if d not in DOC_TYPES]
    if unknown:
        print(f"Unknown document type(s): {', '.join(unknown)}. Choose from: {', '.join(DOC_TYPES)}.")
        return 2
    sources = Sources(spec=_read(args.spec), previous_spec=_read(args.previous), code_diff=_read(args.diff),
                      screenshots=[_image(p) for p in args.screenshot])
    settings = DocSettings(product=args.product, tone=args.tone, language=args.language)
    choice = LLMChoice(provider=args.provider, model=args.model)
    try:
        extracted = extract(sources, settings, choice)
        facts = extracted.facts
        drafts = []
        for doc_type in doc_types:
            if doc_type == "whats_changed" and not (facts.changes or sources.previous_spec):
                print("Skipping whats_changed: no previous version given.")
                continue
            print(f"Drafting {DOC_TYPES[doc_type]['label']}...")
            drafts.append(generate(GenerateRequest(facts=facts, doc_type=doc_type, settings=settings, llm=choice,
                                                   sources=sources, kb_matches=extracted.kb_matches,
                                                   fact_check=not args.no_fact_check)))
    except (LLMError, UserInputError, config.ConfigError) as exc:
        print(f"Error: {exc}")
        return 2

    from .audit import build_record

    record = build_record(facts, drafts, settings, sources, extracted.meta)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "facts.json").write_text(facts.model_dump_json(indent=2), encoding="utf-8")
    for d in drafts:
        (args.out / f"{d.doc_type}.md").write_text(d.markdown, encoding="utf-8")
    (args.out / "check-report.md").write_text(check_report(drafts, record), encoding="utf-8")
    (args.out / "audit.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    worst = 0
    print(f"\nFact sheet: {facts.feature_name} ({len(facts.open_questions)} open questions)")
    for d in drafts:
        marks = ", ".join(f"{c.label}: {c.status}" for c in d.checks if c.status in ("warn", "fail"))
        worst = max([worst] + [LEVELS[c.status] for c in d.checks])
        print(f"  {d.doc_type:<14} {marks or 'all checks passed'}")
    print(f"\nWrote {len(drafts)} draft(s), facts.json, check-report.md and audit.json to {args.out}/")
    threshold = {"never": 99, "warn": 1, "fail": 2}[args.fail_on]
    return 1 if worst >= threshold else 0


def evaluate(args: argparse.Namespace) -> int:
    from .errors import UserInputError
    from .evals import METRICS, RATIOS, baseline_path, compare, render_report, run, save_baseline
    from .schemas import LLMChoice

    choice = LLMChoice(provider=args.provider, model=args.model)
    only = [c.strip() for c in args.cases.split(",") if c.strip()] if args.cases else None
    try:
        llm = config.resolve(args.provider, args.model)
        print(f"Evaluating with {llm.provider} ({llm.model})")
        report = run(choice, only=only, fact_check=not args.no_fact_check)
    except (config.ConfigError, UserInputError) as exc:
        print(f"Error: {exc}")
        return 2

    path = args.baseline or baseline_path(report["provider"], report["model"])
    baseline = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    rows = compare(report["metrics"], baseline)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "eval-report.md").write_text(render_report(report, rows, path if baseline else None), encoding="utf-8")
    (args.out / "eval-results.json").write_text(json.dumps({"report": report, "comparison": rows}, indent=2),
                                                encoding="utf-8")

    def fmt(name: str, value) -> str:
        return "n/a" if value is None else f"{value * 100:.0f}%" if name in RATIOS else f"{value:g}"

    print(f"\n  {'Metric':<40}{'Now':>8}{'Baseline':>10}")
    for row in rows:
        flag = f"  <- {row['reason']}" if row["regressed"] else ""
        print(f"  {METRICS[row['metric']][0]:<40}{fmt(row['metric'], row['now']):>8}"
              f"{fmt(row['metric'], row['baseline']):>10}{flag}")
    print(f"\nReport: {args.out / 'eval-report.md'}")
    if args.save_baseline:
        save_baseline(report, path)
        print(f"Saved these scores as the baseline: {path}")
        return 0
    if baseline is None:
        print("No baseline for this model yet. Run again with --save-baseline to create one.")
    regressions = [row for row in rows if row["regressed"]]
    if regressions:
        print(f"{len(regressions)} regression(s): " + ", ".join(METRICS[r['metric']][0] for r in regressions))
        return 1
    print("No regressions.")
    return 0


def audit_log(args: argparse.Namespace) -> int:
    from .audit import AuditLog

    log = AuditLog(args.dir or config.AUDIT_DIR)
    if args.verify:
        intact, count, problems = log.verify()
        if intact:
            noun = "entry" if count == 1 else "entries"
            print(f"Audit log intact: {count} {noun}, hash chain and bundles verified ({log.path}).")
            return 0
        print(f"Audit log check failed ({count} entries in {log.path}):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    entries = log.entries()
    if not entries:
        print(f"No exports recorded yet ({log.path}).")
        return 0
    for entry in entries[-args.limit:]:
        record, summary = entry["record"], entry["record"]["summary"]
        approvers = sorted({d["approval"]["by"] for d in record["documents"]
                            if d["approval"] and d["approval"]["valid"]})
        line = (f"{record['exported_at']}  {record['id']}  {record['feature']}: "
                f"{summary['approved']} of {summary['documents']} approved")
        if approvers:
            line += f" by {', '.join(approvers)}"
        if summary["approved_with_failures"]:
            line += f", {summary['approved_with_failures']} despite failing checks"
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="proofmark",
                                     description="Turn a feature spec into support documentation.")
    sub = parser.add_subparsers(dest="command")

    serve_cmd = sub.add_parser("serve", help="start the web app (the default)")
    serve_cmd.add_argument("--host", default=config.HOST)
    serve_cmd.add_argument("--port", type=int, default=config.PORT)
    serve_cmd.add_argument("--no-browser", action="store_true", help="do not open a browser tab")

    draft_cmd = sub.add_parser("draft", help="draft documents from files, without the UI (for CI)")
    draft_cmd.add_argument("--spec", type=Path, required=True, help="feature spec (Markdown or text)")
    draft_cmd.add_argument("--previous", type=Path, help="previous version of the spec")
    draft_cmd.add_argument("--diff", type=Path, help="code changes as a unified diff")
    draft_cmd.add_argument("--screenshot", type=Path, action="append", default=[], help="repeatable")
    draft_cmd.add_argument("--docs", default="help_article,faq,release_notes,agent_brief",
                           help="comma-separated: help_article, faq, release_notes, agent_brief, whats_changed")
    draft_cmd.add_argument("--out", type=Path, default=Path("proofmark-output"))
    draft_cmd.add_argument("--provider", help="demo, gemini, groq, ollama or custom (default: from .env)")
    draft_cmd.add_argument("--model")
    draft_cmd.add_argument("--product", default="Blitz")
    draft_cmd.add_argument("--tone", default="friendly", choices=["friendly", "neutral", "playful"])
    draft_cmd.add_argument("--language", default="English")
    draft_cmd.add_argument("--no-fact-check", action="store_true", help="skip the AI fact-check call")
    draft_cmd.add_argument("--fail-on", default="fail", choices=["never", "warn", "fail"],
                           help="exit with code 1 when any check reaches this level (default: fail)")

    eval_cmd = sub.add_parser("eval", help="score the golden cases and compare with the saved baseline")
    eval_cmd.add_argument("--provider", help="demo, gemini, groq, ollama or custom (default: from .env)")
    eval_cmd.add_argument("--model")
    eval_cmd.add_argument("--cases", help="comma-separated case ids (default: all in evals/cases)")
    eval_cmd.add_argument("--baseline", type=Path, help="baseline file (default: evals/baselines/<provider>--<model>.json)")
    eval_cmd.add_argument("--save-baseline", action="store_true", help="accept the current scores as the baseline")
    eval_cmd.add_argument("--no-fact-check", action="store_true", help="skip the AI fact-check calls")
    eval_cmd.add_argument("--out", type=Path, default=Path("proofmark-output/eval"))

    audit_cmd = sub.add_parser("audit", help="list recorded exports, or verify the audit log")
    audit_cmd.add_argument("--verify", action="store_true", help="check the hash chain and the stored bundles")
    audit_cmd.add_argument("--limit", type=int, default=20)
    audit_cmd.add_argument("--dir", type=Path, help="audit directory (default: audit/)")

    args = parser.parse_args(argv)
    if args.command == "draft":
        return draft(args)
    if args.command == "eval":
        return evaluate(args)
    if args.command == "audit":
        return audit_log(args)
    host = getattr(args, "host", config.HOST)
    port = getattr(args, "port", config.PORT)
    serve(host, port, open_browser=not getattr(args, "no_browser", False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
