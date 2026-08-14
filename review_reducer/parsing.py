"""Normalize Codex's native JSON-shaped or rendered review output."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from review_reducer.errors import InvalidReviewError
from review_reducer.git import safe_repo_path
from review_reducer.models import Finding


_PRIORITY = re.compile(r"\[P(?P<priority>[0-3])\]", re.IGNORECASE)
_RENDERED_FINDING = re.compile(
    r"^\s*-\s+(?:\[[ x]\]\s+)?(?P<title>.*?)\s+[—–]\s+"
    r"(?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?\s*$"
)


def _json_review(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    stripped = text.strip()
    for position, character in enumerate(stripped):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[position:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("findings"), list):
            return value
    return None


def _priority(title: str, value: Any = None) -> int:
    if isinstance(value, int) and 0 <= value <= 3:
        return value
    match = _PRIORITY.search(title)
    return int(match.group("priority")) if match else 2


def _relative_path(repo: Path, raw: str) -> str:
    return str(safe_repo_path(repo, raw).relative_to(repo.resolve()))


def _parse_json_findings(review: dict[str, Any], repo: Path) -> list[Finding]:
    results: list[Finding] = []
    for item in review["findings"]:
        if not isinstance(item, dict):
            raise InvalidReviewError("native review contains a non-object finding")
        location = item.get("code_location") or {}
        line_range = location.get("line_range") or {}
        if not location.get("absolute_file_path"):
            raise InvalidReviewError("native finding is missing its source location")
        title = str(item.get("title", "")).strip()
        if not title:
            raise InvalidReviewError("native finding is missing its title")
        results.append(
            Finding(
                title=title,
                body=str(item.get("body", "")).strip(),
                path=_relative_path(repo, str(location["absolute_file_path"])),
                line_start=int(line_range.get("start", 1)),
                line_end=int(line_range.get("end", line_range.get("start", 1))),
                priority=_priority(title, item.get("priority")),
                confidence=float(item.get("confidence_score", 0.0)),
            )
        )
    return results


def _parse_rendered_findings(text: str, repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    pending: dict[str, Any] | None = None
    body_lines: list[str] = []

    def finish() -> None:
        nonlocal pending, body_lines
        if pending is None:
            return
        findings.append(
            Finding(
                title=str(pending["title"]).strip(),
                body="\n".join(body_lines).strip(),
                path=_relative_path(repo, str(pending["path"])),
                line_start=int(pending["start"]),
                line_end=int(pending["end"] or pending["start"]),
                priority=_priority(str(pending["title"])),
            )
        )
        pending = None
        body_lines = []

    for line in text.splitlines():
        match = _RENDERED_FINDING.match(line)
        if match:
            finish()
            pending = match.groupdict()
        elif pending is not None:
            if line.startswith("  "):
                body_lines.append(line[2:])
            elif line.strip():
                body_lines.append(line.strip())
    finish()
    return findings


def parse_native_review(text: str, repo: Path) -> list[Finding]:
    stripped = text.strip()
    if not stripped:
        raise InvalidReviewError("native review did not produce a final response")
    if stripped == "Reviewer failed to output a response.":
        raise InvalidReviewError("native reviewer did not produce a trustworthy response")
    review = _json_review(stripped)
    findings = (
        _parse_json_findings(review, repo)
        if review is not None
        else _parse_rendered_findings(stripped, repo)
    )
    if not findings and _PRIORITY.search(stripped):
        raise InvalidReviewError(
            "native review contains a priority-tagged finding that could not be parsed"
        )
    deduplicated: dict[str, Finding] = {}
    for finding in findings:
        existing = deduplicated.get(finding.finding_id)
        if existing is None or finding.priority < existing.priority:
            deduplicated[finding.finding_id] = finding
    return sorted(deduplicated.values(), key=lambda finding: (finding.priority, finding.path))
