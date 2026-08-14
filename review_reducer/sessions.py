"""Durable, inspectable review sessions and explicit human overrides."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import threading
from typing import Any

from review_reducer.errors import ReviewReducerError
from review_reducer.git import git_common_dir, repository_root
from review_reducer.models import Challenge, Decision, Finding, Observation, Snapshot, Verdict


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReviewReducerError(f"cannot read saved review session {path}: {error}") from error
    if not isinstance(value, dict):
        raise ReviewReducerError(f"saved review session is not a JSON object: {path}")
    return value


def _effective_verdict(entry: dict[str, Any]) -> str:
    if entry.get("resolved"):
        return "resolved"
    override = entry.get("manual_override")
    if override:
        return "accept" if override["action"] == "include" else "reject"
    decision = entry.get("decision")
    return str(decision["verdict"]) if decision else "pending"


def _summarize(findings: list[dict[str, Any]]) -> dict[str, int]:
    result = {
        "total": len(findings),
        "accepted": 0,
        "rejected": 0,
        "non_blocking": 0,
        "human_review": 0,
        "pending": 0,
        "resolved": 0,
        "manually_included": 0,
        "manually_dismissed": 0,
    }
    for entry in findings:
        verdict = _effective_verdict(entry)
        key = {
            "accept": "accepted",
            "reject": "rejected",
            "non_blocking": "non_blocking",
            "human_review": "human_review",
            "pending": "pending",
            "resolved": "resolved",
        }.get(verdict, "pending")
        result[key] += 1
        override = entry.get("manual_override")
        if override:
            result[
                "manually_included"
                if override["action"] == "include"
                else "manually_dismissed"
            ] += 1
    return result


def _status(findings: list[dict[str, Any]]) -> str:
    verdicts = {_effective_verdict(entry) for entry in findings}
    if "human_review" in verdicts or "pending" in verdicts:
        return "human_review_required"
    if "accept" in verdicts:
        return "action_required"
    return "clean"


def _legacy_session(run_dir: Path) -> dict[str, Any]:
    report = _read_json(run_dir / "report.json")
    snapshot = report.get("snapshot") or {}
    entries: dict[str, dict[str, Any]] = {}
    for finding in report.get("initial_findings", []):
        finding_id = str(finding["finding_id"])
        entries[finding_id] = {
            "finding": finding,
            "phases": ["initial"],
            "investigations": {},
            "decisions": {},
            "decision": None,
            "manual_override": None,
            "history": [],
        }
    for phase in ("initial", "final"):
        for decision in report.get(f"{phase}_decisions", []):
            finding = decision["finding"]
            finding_id = str(finding["finding_id"])
            entry = entries.setdefault(
                finding_id,
                {
                    "finding": finding,
                    "phases": [],
                    "investigations": {},
                    "decisions": {},
                    "decision": None,
                    "manual_override": None,
                    "history": [],
                },
            )
            if phase not in entry["phases"]:
                entry["phases"].append(phase)
            entry["decisions"][phase] = decision
            entry["decision"] = decision
            entry["investigations"][phase] = {
                "observation": decision.get("observation"),
                "adversary": decision.get("adversarial_challenge") or decision.get("challenge"),
                "reviewer_response": decision.get("reviewer_response"),
            }
    if report.get("repair") is not None:
        final_ids = {
            str(finding["finding_id"])
            for finding in report.get("final_findings", [])
        }
        repaired_ids = {
            str(finding_id)
            for finding_id in report["repair"].get("applied_finding_ids", [])
        }
        for finding_id, entry in entries.items():
            entry["resolved"] = finding_id in repaired_ids and finding_id not in final_ids
    findings = list(entries.values())
    return {
        "version": 1,
        "session_id": run_dir.name,
        "created_at": "",
        "updated_at": "",
        "state": "complete",
        "status": _status(findings),
        "mode": report.get("mode", "review"),
        "snapshot": snapshot,
        "findings": findings,
        "summary": _summarize(findings),
        "usage": report.get("usage", {}),
        "artifacts_dir": str(run_dir),
        "report_path": str(run_dir / "report.json"),
    }


class ReviewSession:
    """Persist each review observation and human decision as it happens."""

    def __init__(self, run_dir: Path, data: dict[str, Any]) -> None:
        self.run_dir = run_dir.resolve()
        self.path = self.run_dir / "session.json"
        self.data = data
        self._lock = threading.RLock()

    @classmethod
    def create(cls, run_dir: Path, snapshot: Snapshot, mode: str) -> "ReviewSession":
        now = _timestamp()
        session = cls(
            run_dir,
            {
                "version": 1,
                "session_id": run_dir.name,
                "created_at": now,
                "updated_at": now,
                "state": "running",
                "status": "running",
                "mode": mode,
                "snapshot": snapshot.to_dict(),
                "findings": [],
                "summary": _summarize([]),
                "usage": {},
                "artifacts_dir": str(run_dir),
                "report_path": str(run_dir / "report.json"),
            },
        )
        session.save()
        return session

    @classmethod
    def open(cls, run_dir: Path) -> "ReviewSession":
        run_dir = run_dir.resolve()
        path = run_dir / "session.json"
        return cls(run_dir, _read_json(path) if path.is_file() else _legacy_session(run_dir))

    def save(self) -> None:
        with self._lock:
            self.data["updated_at"] = _timestamp()
            self.data["summary"] = _summarize(self.data["findings"])
            if self.data["state"] != "running":
                self.data["status"] = _status(self.data["findings"])
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            temporary.replace(self.path)

    def _entry(self, finding_id: str) -> dict[str, Any]:
        for entry in self.data["findings"]:
            if entry["finding"]["finding_id"] == finding_id:
                return entry
        raise ReviewReducerError(f"finding is not present in this review session: {finding_id}")

    def record_findings(self, findings: tuple[Finding, ...], phase: str) -> None:
        with self._lock:
            for finding in findings:
                existing = next(
                    (
                        entry
                        for entry in self.data["findings"]
                        if entry["finding"]["finding_id"] == finding.finding_id
                    ),
                    None,
                )
                if existing:
                    if phase not in existing["phases"]:
                        existing["phases"].append(phase)
                    continue
                self.data["findings"].append(
                    {
                        "finding": finding.to_dict(),
                        "phases": [phase],
                        "investigations": {},
                        "decisions": {},
                        "decision": None,
                        "manual_override": None,
                        "history": [],
                        "resolved": False,
                    }
                )
            self.save()

    def record_investigation(
        self,
        finding: Finding,
        phase: str,
        *,
        observation: Observation | None = None,
        adversary: Challenge | None = None,
        reviewer_response: Challenge | None = None,
    ) -> None:
        with self._lock:
            entry = self._entry(finding.finding_id)
            investigation = entry["investigations"].setdefault(phase, {})
            if observation:
                investigation["observation"] = observation.to_dict()
            if adversary:
                investigation["adversary"] = adversary.to_dict()
            if reviewer_response:
                investigation["reviewer_response"] = reviewer_response.to_dict()
            self.save()

    def record_decision(self, decision: Decision, phase: str) -> None:
        with self._lock:
            entry = self._entry(decision.finding.finding_id)
            payload = decision.to_dict()
            entry["decisions"][phase] = payload
            entry["decision"] = payload
            entry["history"].append(
                {
                    "at": _timestamp(),
                    "phase": phase,
                    "action": "model_decision",
                    "verdict": decision.verdict.value,
                    "reason": decision.reason,
                }
            )
            self.save()

    def complete(self, report: dict[str, Any]) -> None:
        with self._lock:
            if report.get("repair") is not None:
                final_ids = {
                    str(finding["finding_id"])
                    for finding in report.get("final_findings", [])
                }
                repaired_ids = {
                    str(finding_id)
                    for finding_id in report["repair"].get("applied_finding_ids", [])
                }
                for entry in self.data["findings"]:
                    finding_id = entry["finding"]["finding_id"]
                    entry["resolved"] = (
                        finding_id in repaired_ids and finding_id not in final_ids
                    )
            self.data["state"] = "complete"
            self.data["usage"] = report.get("usage", {})
            self.data["report_path"] = str(self.run_dir / "report.json")
            self.save()

    def fail(self, reason: str) -> None:
        with self._lock:
            self.data["state"] = "failed"
            self.data["failure"] = reason
            self.save()

    def resolve_finding(self, selector: str) -> dict[str, Any]:
        entries = self.data["findings"]
        if selector.isdecimal() and 1 <= int(selector) <= len(entries):
            return entries[int(selector) - 1]
        matches = [
            entry for entry in entries if entry["finding"]["finding_id"].startswith(selector)
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ReviewReducerError(f"no finding matches {selector!r} in this review session")
        raise ReviewReducerError(f"finding selector {selector!r} is ambiguous")

    def override(self, selector: str, action: str, reason: str = "") -> dict[str, Any]:
        if action not in {"include", "dismiss", "reset"}:
            raise ReviewReducerError(f"unsupported manual review action: {action}")
        with self._lock:
            entry = self.resolve_finding(selector)
            if action == "reset":
                entry["manual_override"] = None
            else:
                entry["manual_override"] = {
                    "action": action,
                    "reason": reason
                    or {
                        "include": "manually included after reviewing the evidence",
                        "dismiss": "manually dismissed after reviewing the evidence",
                    }[action],
                    "at": _timestamp(),
                }
            entry["history"].append(
                {
                    "at": _timestamp(),
                    "phase": "manual",
                    "action": action,
                    "reason": reason,
                }
            )
            self.data["state"] = "complete"
            self.save()
            return entry

    def effective_decisions(self) -> tuple[Decision, ...]:
        results: list[Decision] = []
        for entry in self.data["findings"]:
            payload = entry.get("decision")
            if not payload:
                continue
            decision = Decision.from_dict(payload)
            override = entry.get("manual_override")
            if override and override["action"] == "dismiss":
                decision = replace(
                    decision,
                    verdict=Verdict.REJECT,
                    reason=override["reason"],
                    blocks_review=False,
                    auto_fix_allowed=False,
                )
            elif override and override["action"] == "include":
                challenge = decision.challenge
                decision = replace(
                    decision,
                    verdict=Verdict.ACCEPT,
                    reason=override["reason"],
                    blocks_review=True,
                    auto_fix_allowed=bool(
                        challenge
                        and challenge.smallest_fix.strip()
                        and challenge.preserves_change_intent
                        and not challenge.requires_new_dependency
                        and not challenge.requires_new_public_api
                    ),
                )
            results.append(decision)
        return tuple(results)


def list_sessions(repo: Path) -> list[ReviewSession]:
    root = repository_root(repo)
    sessions_root = git_common_dir(root) / "review-reducer"
    if not sessions_root.is_dir():
        return []
    sessions: list[ReviewSession] = []
    for directory in sorted(sessions_root.iterdir(), reverse=True):
        if not directory.is_dir():
            continue
        if not (directory / "session.json").is_file() and not (directory / "report.json").is_file():
            continue
        try:
            session = ReviewSession.open(directory)
        except ReviewReducerError:
            continue
        if Path(str(session.data.get("snapshot", {}).get("repo_root", ""))).resolve() == root:
            sessions.append(session)
    return sessions


def resolve_session(repo: Path, selector: str) -> ReviewSession:
    candidate = Path(selector).expanduser()
    if candidate.is_dir():
        return ReviewSession.open(candidate)
    sessions = list_sessions(repo)
    if selector == "latest":
        if not sessions:
            raise ReviewReducerError("no saved review sessions exist for this repository")
        return sessions[0]
    matches = [session for session in sessions if session.data["session_id"].startswith(selector)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ReviewReducerError(f"no saved review session matches {selector!r}")
    raise ReviewReducerError(f"saved review session selector {selector!r} is ambiguous")


def format_session(session: ReviewSession, finding: dict[str, Any] | None = None) -> str:
    data = session.data
    snapshot = data["snapshot"]
    summary = data["summary"]
    lines = [
        f"Session: {data['session_id']}",
        f"Repository: {snapshot.get('repo_root', '')}",
        f"Head: {str(snapshot.get('head_sha', ''))[:12]}",
        f"State: {data['state']} / {data['status']}",
        "Findings: "
        f"{summary['total']} total, {summary['accepted']} included, "
        f"{summary['rejected']} dismissed, {summary['non_blocking']} non-blocking, "
        f"{summary['resolved']} resolved, "
        f"{summary['human_review']} human review",
    ]
    if finding is None:
        for index, entry in enumerate(data["findings"], start=1):
            item = entry["finding"]
            marker = " (manual)" if entry.get("manual_override") else ""
            lines.append(
                f"  {index}. P{item['priority']} {_effective_verdict(entry)}{marker} "
                f"{item['finding_id'][:10]}  {item['title']}"
            )
        lines.append(f"Artifacts: {session.run_dir}")
        return "\n".join(lines)

    item = finding["finding"]
    decision = finding.get("decision") or {}
    investigations = finding.get("investigations") or {}
    latest_phase = "final" if "final" in investigations else "initial"
    investigation = investigations.get(latest_phase, {})
    lines.extend(
        [
            "",
            f"Finding: P{item['priority']} {item['title']}",
            f"Identifier: {item['finding_id']}",
            f"Location: {item['path']}:{item['line_start']}-{item['line_end']}",
            "",
            "Original reviewer:",
            item.get("body", ""),
        ]
    )
    observation = investigation.get("observation")
    if observation:
        lines.extend(["", "Blind investigation:", observation.get("changed_behavior", "")])
        for anchor in observation.get("source_anchors", []):
            lines.append(
                f"Source: {anchor['path']}:{anchor['line']} "
                f"{anchor.get('explanation', '')}".rstrip()
            )
        if observation.get("realistic_trigger"):
            lines.append("Trigger: " + observation["realistic_trigger"])
        if observation.get("user_impact"):
            lines.append("Impact: " + observation["user_impact"])
    adversary = investigation.get("adversary")
    response = investigation.get("reviewer_response")
    for label, assessment in (
        ("Adversary", adversary),
        ("Reviewer response", response),
    ):
        if not assessment:
            continue
        lines.extend(
            [
                "",
                f"{label}: {assessment.get('assessment', 'unknown')}",
                assessment.get("rationale", ""),
            ]
        )
        for anchor in assessment.get("source_anchors", []):
            lines.append(
                f"Source: {anchor['path']}:{anchor['line']} "
                f"{anchor.get('explanation', '')}".rstrip()
            )
        if assessment.get("realistic_trigger"):
            lines.append("Trigger: " + assessment["realistic_trigger"])
        if assessment.get("user_impact"):
            lines.append("Impact: " + assessment["user_impact"])
        if assessment.get("smallest_fix"):
            lines.append("Smallest fix: " + assessment["smallest_fix"])
        if "confidence" in assessment:
            lines.append(f"Confidence: {assessment['confidence']:.0%}")
    if decision:
        lines.extend(["", "Final model decision: " + str(decision.get("verdict")), decision.get("reason", "")])
    override = finding.get("manual_override")
    if override:
        lines.extend(
            [
                "",
                f"Manual override: {override['action']}",
                override.get("reason", ""),
            ]
        )
    lines.append("")
    lines.append("Effective decision: " + _effective_verdict(finding))
    return "\n".join(lines)
