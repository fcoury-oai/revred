"""Command-line interface for the standalone review reducer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from review_reducer import __version__
from review_reducer.errors import ReviewReducerError
from review_reducer.policy import ReviewPolicy
from review_reducer.workflow import RunConfig, ReviewWorkflow, format_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-reducer",
        description="Adversarial, evidence-grounded Codex pull-request review",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser("review", help="review or conservatively repair a Git branch")
    review.add_argument("--repo", type=Path, default=Path.cwd(), help="target Git repository")
    review.add_argument("--base", default="origin/main", help="base branch or commit")
    review.add_argument("--mode", choices=("review", "fix"), default="review")
    review.add_argument("--max-priority", type=int, choices=range(4), default=1)
    review.add_argument("--min-confidence", type=float, default=0.75)
    review.add_argument("--max-added-production-lines", type=int, default=20)
    review.add_argument("--max-additional-production-files", type=int, default=2)
    review.add_argument("--max-findings", type=int, default=12)
    review.add_argument("--jobs", type=int, default=2)
    review.add_argument("--timeout", type=int, default=1200, help="per-Codex-call seconds")
    review.add_argument("--artifacts-dir", type=Path)
    review.add_argument("--review-file", type=Path, help="reuse an existing native review")
    review.add_argument("--codex-bin", default="codex")
    review.add_argument("--review-model")
    review.add_argument("--verifier-model")
    review.add_argument("--fixer-model")
    review.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh"))
    review.add_argument("--no-blind-verification", action="store_true")
    review.add_argument(
        "--progress",
        choices=("auto", "always", "never"),
        default="auto",
        help="live terminal dashboard behavior (default: auto)",
    )
    review.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="COMMAND",
        help="run this explicit check after repair; no shell interpretation",
    )
    review.add_argument("--json", action="store_true", help="print the full JSON report")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    if not 0 <= options.min_confidence <= 1:
        parser.error("--min-confidence must be between 0 and 1")
    for name in (
        "max_added_production_lines",
        "max_additional_production_files",
        "max_findings",
        "jobs",
        "timeout",
    ):
        if getattr(options, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be greater than zero")
    if options.check and options.mode != "fix":
        parser.error("--check is only available with --mode fix")
    config = RunConfig(
        repo=options.repo.resolve(),
        base=options.base,
        mode=options.mode,
        artifacts_dir=options.artifacts_dir,
        review_file=options.review_file,
        codex_bin=options.codex_bin,
        review_model=options.review_model,
        verifier_model=options.verifier_model,
        fixer_model=options.fixer_model,
        reasoning_effort=options.reasoning_effort,
        timeout_seconds=options.timeout,
        jobs=options.jobs,
        max_findings=options.max_findings,
        blind_verification=not options.no_blind_verification,
        checks=tuple(options.check),
        progress=options.progress,
        policy=ReviewPolicy(
            max_priority=options.max_priority,
            min_confidence=options.min_confidence,
            max_added_production_lines=options.max_added_production_lines,
            max_additional_production_files=options.max_additional_production_files,
        ),
    )
    try:
        report = ReviewWorkflow(config).run()
    except ReviewReducerError as error:
        print(f"review-reducer: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2) if options.json else format_report(report))
    return {"clean": 0, "action_required": 2, "human_review_required": 3}[report["status"]]
