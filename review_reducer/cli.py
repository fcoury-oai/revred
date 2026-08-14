"""Command-line interface for the standalone review reducer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from review_reducer import __version__
from review_reducer.errors import ReviewReducerError
from review_reducer.policy import ReviewPolicy
from review_reducer.sessions import format_session, list_sessions, resolve_session
from review_reducer.workflow import RunConfig, ReviewWorkflow, format_report


def _add_repository_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(), help="target Git repository"
    )


def _add_execution_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--min-confidence", type=float, default=0.75)
    parser.add_argument("--max-added-production-lines", type=int, default=20)
    parser.add_argument("--max-additional-production-files", type=int, default=2)
    parser.add_argument("--max-findings", type=int, default=12)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=1200, help="per-Codex-call seconds")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--review-model")
    parser.add_argument("--verifier-model")
    parser.add_argument("--fixer-model")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh"))
    parser.add_argument("--no-blind-verification", action="store_true")
    parser.add_argument(
        "--progress",
        choices=("auto", "always", "never"),
        default="auto",
        help="live terminal dashboard behavior (default: auto)",
    )
    parser.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="COMMAND",
        help="run this explicit check after repair; no shell interpretation",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-reducer",
        description="Adversarial, evidence-grounded Codex pull-request review",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    review = commands.add_parser("review", help="review or conservatively repair a Git branch")
    _add_repository_option(review)
    review.add_argument("--base", default="origin/main", help="base branch or commit")
    review.add_argument("--mode", choices=("review", "fix"), default="review")
    review.add_argument("--artifacts-dir", type=Path)
    review.add_argument("--review-file", type=Path, help="reuse an existing native review")
    _add_execution_options(review)

    session = commands.add_parser("session", help="inspect or curate saved review sessions")
    session_commands = session.add_subparsers(dest="session_command", required=True)

    listing = session_commands.add_parser("list", help="list this repository's saved sessions")
    _add_repository_option(listing)
    listing.add_argument("--json", action="store_true")

    show = session_commands.add_parser("show", help="inspect a session or one finding")
    show.add_argument("session", help="latest, a session-ID prefix, or an artifact directory")
    _add_repository_option(show)
    show.add_argument("--finding", help="1-based finding number or finding-ID prefix")
    show.add_argument("--json", action="store_true")

    for action in ("include", "dismiss", "reset"):
        command = session_commands.add_parser(
            action, help=f"{action} a saved finding after inspecting its evidence"
        )
        command.add_argument("session", help="latest, a session-ID prefix, or a directory")
        command.add_argument("finding", help="1-based finding number or finding-ID prefix")
        _add_repository_option(command)
        command.add_argument("--reason", default="", help="record why this override is useful")
        command.add_argument("--json", action="store_true")

    apply = session_commands.add_parser(
        "apply", help="apply one bounded repair batch from a curated saved session"
    )
    apply.add_argument("session", help="latest, a session-ID prefix, or a directory")
    _add_repository_option(apply)
    _add_execution_options(apply)
    return parser


def _validate_execution_options(
    parser: argparse.ArgumentParser, options: argparse.Namespace
) -> None:
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
    if options.check and getattr(options, "mode", "fix") != "fix":
        parser.error("--check is only available with --mode fix")


def _run_config(options: argparse.Namespace, *, base: str, mode: str) -> RunConfig:
    return RunConfig(
        repo=options.repo.resolve(),
        base=base,
        mode=mode,
        artifacts_dir=getattr(options, "artifacts_dir", None),
        review_file=getattr(options, "review_file", None),
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
            min_confidence=options.min_confidence,
            max_added_production_lines=options.max_added_production_lines,
            max_additional_production_files=options.max_additional_production_files,
        ),
    )


def _emit_report(report: dict[str, object], *, as_json: bool) -> int:
    print(json.dumps(report, indent=2) if as_json else format_report(report))
    return {"clean": 0, "action_required": 2, "human_review_required": 3}[
        str(report["status"])
    ]


def _session_command(
    parser: argparse.ArgumentParser, options: argparse.Namespace
) -> int:
    repo = options.repo.resolve()
    if options.session_command == "list":
        sessions = list_sessions(repo)
        if options.json:
            print(json.dumps([session.data for session in sessions], indent=2))
        elif sessions:
            for saved in sessions:
                data = saved.data
                print(
                    f"{data['session_id']}  {data['status']}  "
                    f"{data['summary']['total']} findings  "
                    f"{str(data['snapshot'].get('head_sha', ''))[:12]}"
                )
        else:
            print("No saved review sessions exist for this repository.")
        return 0

    saved = resolve_session(repo, options.session)
    if options.session_command == "show":
        entry = saved.resolve_finding(options.finding) if options.finding else None
        print(
            json.dumps(entry if entry else saved.data, indent=2)
            if options.json
            else format_session(saved, entry)
        )
        return 0

    if options.session_command in {"include", "dismiss", "reset"}:
        entry = saved.override(options.finding, options.session_command, options.reason)
        if options.json:
            print(json.dumps(entry, indent=2))
        else:
            action = {
                "include": "Included",
                "dismiss": "Dismissed",
                "reset": "Reset",
            }[options.session_command]
            finding = entry["finding"]
            print(
                f"{action} P{finding['priority']} {finding['title']} "
                f"({finding['finding_id'][:10]})"
            )
            print(f"Session status: {saved.data['status']}")
        return 0

    _validate_execution_options(parser, options)
    config = _run_config(
        options, base=str(saved.data["snapshot"]["base_ref"]), mode="fix"
    )
    return _emit_report(ReviewWorkflow(config).apply_session(saved), as_json=options.json)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    try:
        if options.command == "session":
            return _session_command(parser, options)
        _validate_execution_options(parser, options)
        config = _run_config(options, base=options.base, mode=options.mode)
        return _emit_report(ReviewWorkflow(config).run(), as_json=options.json)
    except ReviewReducerError as error:
        print(f"review-reducer: {error}", file=sys.stderr)
        return 1
