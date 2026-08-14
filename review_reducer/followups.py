"""Pinned, read-only follow-up questions about one saved review finding."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from review_reducer.codex import CodexRunner
from review_reducer.display import ProgressDisplay
from review_reducer.errors import ReviewReducerError
from review_reducer.git import capture_snapshot, ensure_snapshot, safe_repo_path
from review_reducer.html_report import write_html_report
from review_reducer.models import Finding, Snapshot, SourceAnchor
from review_reducer.prompts import followup_prompt
from review_reducer.sessions import ReviewSession


def _saved_report(session: ReviewSession) -> dict[str, Any]:
    path = session.run_dir / "report.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReviewReducerError(
            f"cannot load the completed review before asking a question: {error}"
        ) from error
    if not isinstance(report, dict):
        raise ReviewReducerError("the completed review report is not a JSON object")
    return report


def _pinned_snapshot(
    repo: Path, session: ReviewSession, report: dict[str, Any]
) -> tuple[Snapshot, str, tuple[str, ...]]:
    saved = session.data["snapshot"]
    current = capture_snapshot(repo, str(saved["base_ref"]))
    for name in ("repo_root", "head_sha", "base_sha", "merge_base_sha"):
        if str(getattr(current, name)) != str(saved[name]):
            raise ReviewReducerError(
                f"saved session no longer matches the current {name}; run a fresh review"
            )
    expected_patch = str(report.get("reviewed_patch_sha256") or saved["patch_sha256"])
    if current.patch_sha256 != expected_patch:
        raise ReviewReducerError(
            "saved session no longer matches the reviewed tracked patch; run a fresh review"
        )
    expected_untracked = tuple(
        str(path)
        for path in report.get("reviewed_untracked_paths", saved.get("untracked_paths", ()))
    )
    if current.untracked_paths != expected_untracked:
        raise ReviewReducerError(
            "saved session no longer matches the reviewed untracked paths; run a fresh review"
        )
    ensure_snapshot(
        current,
        expected_patch=expected_patch,
        expected_untracked=expected_untracked,
    )
    return current, expected_patch, expected_untracked


def _validate_answer(
    answer: dict[str, Any], finding_id: str, repo: Path
) -> None:
    if answer["finding_id"] != finding_id:
        raise ReviewReducerError("the follow-up answer returned a different finding identifier")
    confidence = answer["confidence"]
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ReviewReducerError("follow-up confidence must be between zero and one")
    if answer["estimated_added_production_lines"] < 0:
        raise ReviewReducerError("the follow-up repair estimate cannot be negative")
    if answer["answer_status"] == "answered" and not answer["answer"].strip():
        raise ReviewReducerError("the follow-up answer cannot be empty")
    anchors = tuple(SourceAnchor.from_dict(item) for item in answer["source_anchors"])
    if answer["evidence_kind"] == "source_grounded" and not anchors:
        raise ReviewReducerError("a source-grounded follow-up answer needs source anchors")
    for anchor in anchors:
        path = safe_repo_path(repo, anchor.path)
        if not path.is_file():
            raise ReviewReducerError(f"follow-up source anchor does not exist: {anchor.path}")
        try:
            line_count = len(path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeError) as error:
            raise ReviewReducerError(
                f"follow-up source anchor cannot be inspected: {anchor.path}: {error}"
            ) from error
        if not 1 <= anchor.line <= max(1, line_count):
            raise ReviewReducerError(
                f"follow-up source anchor line is outside {anchor.path}: {anchor.line}"
            )


def _update_report(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
    except OSError as error:
        raise ReviewReducerError(f"cannot update the saved review report: {error}") from error


def ask_finding(
    session: ReviewSession,
    selector: str,
    question: str,
    *,
    repo: Path,
    perspective: str = "neutral",
    codex_bin: str = "codex",
    model: str | None = None,
    reasoning_effort: str | None = None,
    timeout_seconds: int = 1200,
    progress: str = "auto",
) -> dict[str, Any]:
    """Answer one question in a fresh isolated turn without changing its verdict."""

    normalized_question = question.strip()
    if not normalized_question:
        raise ReviewReducerError("follow-up questions cannot be empty")
    if len(normalized_question) > 8_000:
        raise ReviewReducerError("follow-up questions cannot exceed 8,000 characters")
    if perspective not in {"neutral", "reviewer", "adversary"}:
        raise ReviewReducerError(f"unsupported follow-up perspective: {perspective}")
    if timeout_seconds <= 0:
        raise ReviewReducerError("the follow-up timeout must be greater than zero")
    entry = session.resolve_finding(selector)
    report = _saved_report(session)
    snapshot, expected_patch, expected_untracked = _pinned_snapshot(repo, session, report)
    finding = Finding.from_dict(entry["finding"])
    finding_id = str(entry["finding"]["finding_id"])
    sequence = len(entry.get("questions", [])) + 1
    display = ProgressDisplay(mode=progress)
    display.configure(snapshot, "review")
    display.register_findings((finding,), phase="initial")
    runner = CodexRunner(
        repo=Path(snapshot.repo_root),
        run_dir=session.run_dir,
        binary=codex_bin,
        verifier_model=model,
        reasoning_effort=reasoning_effort,
        timeout_seconds=timeout_seconds,
        event_callback=display.agent_event,
    )
    with display:
        display.note(
            f"answering a focused {perspective} question about "
            f"{finding.path}:{finding.line_start}",
            stage="challenge",
        )
        display.finding_step(finding, "answering")
        response = runner.structured_turn(
            label=f"followup-{finding_id}-{sequence:03d}",
            prompt=followup_prompt(snapshot, entry, normalized_question, perspective),
            schema_name="followup.json",
        )
        ensure_snapshot(
            snapshot,
            expected_patch=expected_patch,
            expected_untracked=expected_untracked,
        )
        _validate_answer(response, finding_id, Path(snapshot.repo_root))
        _pinned_snapshot(repo, session, report)
        usage = runner.usage_summary()
        record = session.record_question(
            finding_id,
            question=normalized_question,
            perspective=perspective,
            response=response,
            usage=usage,
        )
        report["usage"] = usage
        _update_report(session.run_dir / "report.json", report)
        write_html_report(session, report=report)
        display.finish("clean", "follow-up answer recorded")
        return record
