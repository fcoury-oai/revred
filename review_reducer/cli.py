"""Command-line interface for the standalone review reducer."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import webbrowser

from review_reducer import __version__
from review_reducer.errors import ReviewReducerError
from review_reducer.followups import ask_finding
from review_reducer.html_report import write_html_report
from review_reducer.policy import ReviewPolicy
from review_reducer.pull_requests import PullRequestTarget, prepare_pull_request
from review_reducer.sessions import format_session, list_sessions, resolve_session
from review_reducer.workflow import RunConfig, ReviewWorkflow, format_report


def _add_repository_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(), help="target Git repository"
    )


def _add_open_report_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--open-report",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="open the standalone HTML report (default: interactive terminals only)",
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
    _add_open_report_option(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-reducer",
        description="Adversarial, evidence-grounded Codex pull-request review",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    review = commands.add_parser("review", help="review or conservatively repair a Git branch")
    review.add_argument(
        "--repo",
        type=Path,
        default=None,
        metavar="PATH|OWNER/REPO",
        help="local Git checkout, or GitHub owner/repository when used with --pr",
    )
    review.add_argument(
        "--pr",
        metavar="NUMBER|OWNER/REPO#NUMBER|URL",
        help="review the exact current head and base of this GitHub pull request",
    )
    review.add_argument("--gh-bin", default="gh", help=argparse.SUPPRESS)
    review.add_argument("--base", default=None, help="base branch or commit (default: origin/main)")
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

    html_report = session_commands.add_parser(
        "report", help="generate and optionally open a standalone HTML report"
    )
    html_report.add_argument(
        "session", help="latest, a session-ID prefix, or an artifact directory"
    )
    _add_repository_option(html_report)
    html_report.add_argument(
        "--output", type=Path, help="write the self-contained report to this path"
    )
    html_report.add_argument("--json", action="store_true")
    _add_open_report_option(html_report)

    followup = session_commands.add_parser(
        "ask", help="ask a focused read-only question about one saved finding"
    )
    followup.add_argument("session", help="latest, a session-ID prefix, or a directory")
    followup.add_argument("finding", help="1-based finding number or finding-ID prefix")
    followup.add_argument("question", help="the question to answer about this finding")
    _add_repository_option(followup)
    followup.add_argument(
        "--perspective", choices=("neutral", "reviewer", "adversary"), default="neutral"
    )
    followup.add_argument("--codex-bin", default="codex")
    followup.add_argument("--model", help="override the focused follow-up model")
    followup.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh"))
    followup.add_argument("--timeout", type=int, default=1200)
    followup.add_argument("--progress", choices=("auto", "always", "never"), default="auto")
    followup.add_argument("--json", action="store_true")
    _add_open_report_option(followup)

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


def _run_config(
    options: argparse.Namespace,
    *,
    base: str,
    mode: str,
    pull_request: PullRequestTarget | None = None,
) -> RunConfig:
    return RunConfig(
        repo=options.repo.resolve(),
        base=base,
        pull_request=pull_request,
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


def _open_report(path: Path, preference: bool | None) -> None:
    if preference is False:
        return
    if preference is None and (
        not sys.stdout.isatty()
        or not sys.stderr.isatty()
        or os.environ.get("CI")
    ):
        return
    try:
        opened = webbrowser.open(path.resolve().as_uri())
    except (OSError, webbrowser.Error) as error:
        print(f"review-reducer: could not open the HTML report: {error}", file=sys.stderr)
        return
    if not opened:
        print(
            f"review-reducer: no browser accepted the HTML report: {path}",
            file=sys.stderr,
        )


def _emit_report(
    report: dict[str, object], *, as_json: bool, open_report: bool | None = None
) -> int:
    print(json.dumps(report, indent=2) if as_json else format_report(report))
    html_path = report.get("html_report")
    if html_path:
        _open_report(Path(str(html_path)), open_report)
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
    if options.session_command == "ask":
        if options.timeout <= 0:
            parser.error("--timeout must be greater than zero")
        record = ask_finding(
            saved,
            options.finding,
            options.question,
            repo=repo,
            perspective=options.perspective,
            codex_bin=options.codex_bin,
            model=options.model,
            reasoning_effort=options.reasoning_effort,
            timeout_seconds=options.timeout,
            progress=options.progress,
        )
        if options.json:
            print(json.dumps(record, indent=2))
        else:
            answer = record["response"]
            print(f"Question: {record['question']}")
            print(f"Perspective: {record['perspective']}")
            print("")
            print(answer["answer"])
            if answer["recommended_action"].strip():
                print("")
                print(f"Recommended action: {answer['recommended_action']}")
            if answer["smallest_fix"].strip():
                print(
                    f"Smallest fix: {answer['smallest_fix']} "
                    f"({answer['estimated_added_production_lines']} added production lines)"
                )
            if answer["source_anchors"]:
                print("Sources:")
                for anchor in answer["source_anchors"]:
                    print(f"  {anchor['path']}:{anchor['line']} — {anchor['explanation']}")
            if answer["uncertainties"]:
                print("Uncertainties:")
                for uncertainty in answer["uncertainties"]:
                    print(f"  - {uncertainty}")
            print(f"Confidence: {answer['confidence']:.0%}")
            print(
                f"Suggested verdict: {answer['suggested_verdict']} "
                "(advisory; the saved verdict is unchanged)"
            )
            print(f"HTML report: {saved.run_dir / 'report.html'}")
        _open_report(saved.run_dir / "report.html", options.open_report)
        return 0

    if options.session_command == "report":
        html_path = write_html_report(saved, output=options.output)
        if options.json:
            print(
                json.dumps(
                    {"session_id": saved.data["session_id"], "html_report": str(html_path)},
                    indent=2,
                )
            )
        else:
            print(f"HTML report: {html_path}")
        _open_report(html_path, options.open_report)
        return 0

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
        write_html_report(saved)
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
    return _emit_report(
        ReviewWorkflow(config).apply_session(saved),
        as_json=options.json,
        open_report=options.open_report,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    try:
        if options.command == "session":
            return _session_command(parser, options)
        _validate_execution_options(parser, options)
        target = None
        if options.pr:
            if options.base:
                parser.error("--base cannot be combined with --pr; its exact GitHub base is used")
            prepared = prepare_pull_request(
                options.pr,
                repository=options.repo,
                gh_binary=options.gh_bin,
            )
            options.repo = prepared.checkout
            target = prepared.target
            base = target.base_sha
        else:
            options.repo = options.repo or Path.cwd()
            base = options.base or "origin/main"
        config = _run_config(options, base=base, mode=options.mode, pull_request=target)
        return _emit_report(
            ReviewWorkflow(config).run(),
            as_json=options.json,
            open_report=options.open_report,
        )
    except ReviewReducerError as error:
        print(f"review-reducer: {error}", file=sys.stderr)
        return 1
