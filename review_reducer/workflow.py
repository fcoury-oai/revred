"""Bounded discover, challenge, optionally repair, and re-review workflow."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any

from review_reducer.codex import CodexRunner
from review_reducer.errors import BudgetExceededError, ReviewReducerError, SnapshotDriftError
from review_reducer.git import (
    capture_snapshot,
    ensure_head,
    ensure_snapshot,
    git_common_dir,
    measure_churn,
    patch_fingerprint,
    working_tree_status,
)
from review_reducer.models import (
    Challenge,
    Decision,
    Finding,
    Observation,
    Snapshot,
    Verdict,
)
from review_reducer.parsing import parse_native_review
from review_reducer.policy import ReviewPolicy
from review_reducer.prompts import blind_prompt, challenge_prompt, fix_prompt


@dataclass(frozen=True, slots=True)
class RunConfig:
    repo: Path
    base: str = "origin/main"
    mode: str = "review"
    artifacts_dir: Path | None = None
    review_file: Path | None = None
    codex_bin: str = "codex"
    review_model: str | None = None
    verifier_model: str | None = None
    fixer_model: str | None = None
    reasoning_effort: str | None = None
    timeout_seconds: int = 1200
    jobs: int = 2
    max_findings: int = 12
    blind_verification: bool = True
    checks: tuple[str, ...] = ()
    policy: ReviewPolicy = field(default_factory=ReviewPolicy)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _create_run_dir(config: RunConfig, snapshot: Snapshot) -> Path:
    repo = Path(snapshot.repo_root)
    common_dir = git_common_dir(repo)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    root = config.artifacts_dir.resolve() if config.artifacts_dir else common_dir / "review-reducer"
    try:
        root.relative_to(repo)
        inside_repo = True
    except ValueError:
        inside_repo = False
    try:
        root.relative_to(common_dir)
        inside_git = True
    except ValueError:
        inside_git = False
    if inside_repo and not inside_git:
        raise ReviewReducerError(
            "the artifact directory cannot live in the reviewed working tree; "
            "use its Git metadata directory or an external path"
        )
    run_dir = root / f"{stamp}-{snapshot.head_sha[:12]}"
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        run_dir.chmod(0o700)
    except OSError:
        pass
    return run_dir


def _status(decisions: tuple[Decision, ...]) -> str:
    if any(decision.verdict is Verdict.HUMAN_REVIEW for decision in decisions):
        return "human_review_required"
    if any(decision.verdict is Verdict.ACCEPT for decision in decisions):
        return "action_required"
    return "clean"


class ReviewWorkflow:
    def __init__(self, config: RunConfig) -> None:
        self.config = config
        self.snapshot: Snapshot | None = None
        self.run_dir: Path | None = None
        self.runner: CodexRunner | None = None
        self.expected_patch: str | None = None
        self.expected_untracked: tuple[str, ...] | None = None

    def _progress(self, message: str) -> None:
        print(f"review-reducer: {message}", file=sys.stderr, flush=True)

    def _runner(self) -> CodexRunner:
        assert self.runner is not None
        return self.runner

    def _snapshot(self) -> Snapshot:
        assert self.snapshot is not None
        return self.snapshot

    def _run_dir(self) -> Path:
        assert self.run_dir is not None
        return self.run_dir

    def _ensure_snapshot(self) -> None:
        ensure_snapshot(
            self._snapshot(),
            expected_patch=self.expected_patch,
            expected_untracked=self.expected_untracked,
        )

    def _review(self, label: str, review_file: Path | None = None) -> tuple[Finding, ...]:
        snapshot = self._snapshot()
        self._ensure_snapshot()
        if review_file:
            try:
                output = review_file.read_text(encoding="utf-8")
            except OSError as error:
                raise ReviewReducerError(f"cannot read supplied review: {error}") from error
            (self._run_dir() / f"{label}.response.txt").write_text(output, encoding="utf-8")
        else:
            output = self._runner().native_review(snapshot.base_sha, label)
        self._ensure_snapshot()
        findings = tuple(parse_native_review(output, Path(snapshot.repo_root)))
        _write_json(
            self._run_dir() / f"{label}.findings.json",
            [finding.to_dict() for finding in findings],
        )
        return findings

    def _adjudicate_one(
        self,
        finding: Finding,
        history: tuple[Decision, ...],
        phase: str,
    ) -> Decision:
        if finding.priority > self.config.policy.max_priority:
            return Decision(
                finding=finding,
                verdict=Verdict.NON_BLOCKING,
                reason="finding is below the configured blocking priority",
            )
        for previous in history:
            if (
                previous.finding.finding_id == finding.finding_id
                and previous.verdict is Verdict.REJECT
            ):
                return Decision(
                    finding=finding,
                    verdict=Verdict.REJECT,
                    reason=f"already source-refuted in this run: {previous.reason}",
                    challenge=previous.challenge,
                    observation=previous.observation,
                )

        snapshot = self._snapshot()
        try:
            self._ensure_snapshot()
            observation: Observation | None = None
            if self.config.blind_verification:
                observed = self._runner().structured_turn(
                    label=f"{phase}-blind-{finding.finding_id}",
                    prompt=blind_prompt(snapshot, finding),
                    schema_name="observation.json",
                )
                observation = Observation.from_dict(observed)
            self._ensure_snapshot()
            challenged = self._runner().structured_turn(
                label=f"{phase}-defense-{finding.finding_id}",
                prompt=challenge_prompt(snapshot, finding, observation, history),
                schema_name="challenge.json",
            )
            challenge = Challenge.from_dict(challenged)
            self._ensure_snapshot()
            for previous in history:
                if (
                    previous.verdict is Verdict.REJECT
                    and previous.challenge
                    and previous.challenge.root_cause.strip()
                    and previous.challenge.semantic_id == challenge.semantic_id
                ):
                    return Decision(
                        finding=finding,
                        verdict=Verdict.REJECT,
                        reason="the root cause was already source-refuted in this run",
                        challenge=challenge,
                        observation=observation,
                    )
            return self.config.policy.decide(finding, challenge, observation, snapshot)
        except SnapshotDriftError:
            raise
        except (ReviewReducerError, ValueError, TypeError) as error:
            return Decision(
                finding=finding,
                verdict=Verdict.HUMAN_REVIEW,
                reason=f"independent verification failed safely: {error}",
                blocks_review=True,
            )

    def _adjudicate(
        self,
        findings: tuple[Finding, ...],
        history: tuple[Decision, ...] = (),
        *,
        label: str,
    ) -> tuple[Decision, ...]:
        actionable = sum(
            finding.priority <= self.config.policy.max_priority for finding in findings
        )
        if actionable > self.config.max_findings:
            raise ReviewReducerError(
                f"review returned {actionable} potentially blocking findings; "
                f"maximum is {self.config.max_findings}"
            )
        self._progress(f"adjudicating {actionable} potentially blocking findings")
        with ThreadPoolExecutor(max_workers=self.config.jobs) as executor:
            decisions = tuple(
                executor.map(
                    lambda finding: self._adjudicate_one(finding, history, label),
                    findings,
                )
            )
        decisions = self.config.policy.deduplicate(decisions)
        _write_json(
            self._run_dir() / f"{label}.decisions.json",
            [decision.to_dict() for decision in decisions],
        )
        return decisions

    def _run_checks(self) -> tuple[dict[str, Any], ...]:
        results: list[dict[str, Any]] = []
        for index, command in enumerate(self.config.checks, start=1):
            try:
                argv = shlex.split(command)
            except ValueError as error:
                raise ReviewReducerError(f"invalid check command {command!r}: {error}") from error
            if not argv:
                raise ReviewReducerError("explicit check commands cannot be empty")
            self._progress(f"running explicitly requested check: {command}")
            try:
                result = subprocess.run(
                    argv,
                    cwd=self._snapshot().repo_root,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=self.config.timeout_seconds,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as error:
                raise ReviewReducerError(f"explicit check failed: {command}: {error}") from error
            path = self._run_dir() / f"check-{index}.txt"
            path.write_text(result.stdout + result.stderr, encoding="utf-8")
            entry = {"command": command, "exit_code": result.returncode, "output": str(path)}
            results.append(entry)
            if result.returncode:
                raise ReviewReducerError(
                    f"explicit check failed with exit {result.returncode}: {command}; "
                    "working-tree changes were preserved"
                )
        return tuple(results)

    def _repair(self, decisions: tuple[Decision, ...]) -> tuple[dict[str, Any], dict[str, Any]]:
        snapshot = self._snapshot()
        self._ensure_snapshot()
        accepted = tuple(
            decision for decision in decisions if decision.verdict is Verdict.ACCEPT
        )
        self._progress(f"applying one bounded batch of {len(accepted)} verified fixes")
        result = self._runner().structured_turn(
            label="repair",
            prompt=fix_prompt(
                snapshot,
                accepted,
                max_added_production_lines=self.config.policy.max_added_production_lines,
                max_additional_production_files=(
                    self.config.policy.max_additional_production_files
                ),
            ),
            schema_name="fix.json",
            writable=True,
        )
        ensure_head(snapshot)
        expected = {decision.finding.finding_id for decision in accepted}
        claimed = set(result["applied_finding_ids"])
        if not claimed:
            raise ReviewReducerError(
                "the repair role did not claim any verified fix; "
                "working-tree changes were preserved"
            )
        if not claimed.issubset(expected):
            raise ReviewReducerError(
                "the repair role claimed an unauthorized finding; "
                "working-tree changes were preserved"
            )
        dirty, untracked = working_tree_status(Path(snapshot.repo_root))
        if untracked:
            raise BudgetExceededError(
                "automatic repair created untracked files, which native base review "
                "would omit; working-tree changes were preserved: "
                + ", ".join(untracked)
            )
        if not dirty:
            raise ReviewReducerError("the repair role claimed fixes but changed no tracked files")
        churn = measure_churn(Path(snapshot.repo_root), snapshot.head_sha)
        self.config.policy.enforce_repair_budget(churn)
        result["verified_checks"] = list(self._run_checks())
        ensure_head(snapshot)
        current_dirty, current_untracked = working_tree_status(Path(snapshot.repo_root))
        if current_untracked:
            raise BudgetExceededError(
                "an explicit repair check created untracked files; "
                "working-tree changes were preserved: " + ", ".join(current_untracked)
            )
        final_churn = measure_churn(Path(snapshot.repo_root), snapshot.head_sha)
        self.config.policy.enforce_repair_budget(final_churn)
        self.expected_patch = patch_fingerprint(
            Path(snapshot.repo_root), snapshot.merge_base_sha
        )
        self.expected_untracked = current_untracked
        result["actual_changed_files"] = list(current_dirty)
        _write_json(self._run_dir() / "repair.json", result)
        _write_json(self._run_dir() / "repair.churn.json", final_churn.to_dict())
        return result, final_churn.to_dict()

    def _finish(self, report: dict[str, Any]) -> dict[str, Any]:
        report["usage"] = self._runner().usage_summary()
        _write_json(self._run_dir() / "report.json", report)
        summary = format_report(report)
        (self._run_dir() / "summary.md").write_text(summary + "\n", encoding="utf-8")
        return report

    def run(self) -> dict[str, Any]:
        self.snapshot = capture_snapshot(self.config.repo, self.config.base)
        snapshot = self._snapshot()
        self.expected_patch = snapshot.patch_sha256
        self.expected_untracked = snapshot.untracked_paths
        if self.config.mode == "fix" and (snapshot.dirty_paths or snapshot.untracked_paths):
            raise ReviewReducerError(
                "automatic repair requires a clean working tree; commit or stash "
                "tracked changes and remove or ignore untracked files first"
            )
        self.run_dir = _create_run_dir(self.config, snapshot)
        _write_json(self._run_dir() / "snapshot.json", snapshot.to_dict())
        self.runner = CodexRunner(
            repo=Path(snapshot.repo_root),
            run_dir=self._run_dir(),
            binary=self.config.codex_bin,
            review_model=self.config.review_model,
            verifier_model=self.config.verifier_model,
            fixer_model=self.config.fixer_model,
            reasoning_effort=self.config.reasoning_effort,
            timeout_seconds=self.config.timeout_seconds,
        )
        self._progress(f"reviewing pinned head {snapshot.head_sha[:12]}")
        initial_findings = self._review("initial", self.config.review_file)
        initial_decisions = self._adjudicate(initial_findings, label="initial")
        report: dict[str, Any] = {
            "mode": self.config.mode,
            "status": _status(initial_decisions),
            "snapshot": snapshot.to_dict(),
            "initial_findings": [finding.to_dict() for finding in initial_findings],
            "initial_decisions": [decision.to_dict() for decision in initial_decisions],
            "repair": None,
            "repair_churn": None,
            "final_findings": [],
            "final_decisions": [],
            "artifacts_dir": str(self._run_dir()),
        }

        if self.config.mode != "fix" or report["status"] != "action_required":
            return self._finish(report)

        repair, repair_churn = self._repair(initial_decisions)
        report["repair"] = repair
        report["repair_churn"] = repair_churn
        self._progress("running the single bounded final review")
        final_findings = self._review("final")
        final_decisions = self._adjudicate(
            final_findings, initial_decisions, label="final"
        )
        report["final_findings"] = [finding.to_dict() for finding in final_findings]
        report["final_decisions"] = [decision.to_dict() for decision in final_decisions]
        report["status"] = _status(final_decisions)
        return self._finish(report)


def format_report(report: dict[str, Any]) -> str:
    snapshot = report["snapshot"]
    lines = [
        f"Review reducer: {report['status']}",
        f"Repository: {snapshot['repo_root']}",
        f"Base: {snapshot['base_ref']} ({snapshot['merge_base_sha'][:12]})",
        f"Head: {snapshot['head_sha'][:12]}",
        f"Initial findings: {len(report['initial_findings'])}",
    ]
    for decision in report["initial_decisions"]:
        finding = decision["finding"]
        lines.append(
            f"  P{finding['priority']} "
            f"{decision['verdict']}: {finding['title']} ({decision['reason']})"
        )
    if report["repair"]:
        churn = report["repair_churn"]
        lines.extend(
            [
                f"Repair: {report['repair']['summary']}",
                "Repair production churn: "
                f"+{churn['production_added']}/-{churn['production_deleted']} "
                f"across {len(churn['production_files'])} files",
                f"Final findings: {len(report['final_findings'])}",
            ]
        )
        for decision in report["final_decisions"]:
            finding = decision["finding"]
            lines.append(
                f"  P{finding['priority']} {decision['verdict']}: "
                f"{finding['title']} ({decision['reason']})"
            )
    usage = report.get("usage")
    if usage:
        lines.append(
            f"Codex turns: {usage['turn_count']}; "
            f"tokens: {usage['input_tokens']} input "
            f"({usage['cached_input_tokens']} cached), "
            f"{usage['output_tokens']} output"
        )
    lines.append(f"Artifacts: {report['artifacts_dir']}")
    return "\n".join(lines)
