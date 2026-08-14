"""Deterministic evidence gates and hard repair-complexity budgets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from review_reducer.errors import BudgetExceededError, ReviewReducerError
from review_reducer.git import safe_repo_path
from review_reducer.models import (
    Assessment,
    Challenge,
    Churn,
    Decision,
    EvidenceKind,
    Finding,
    Observation,
    Snapshot,
    SourceAnchor,
    Verdict,
)


_GROUNDED = {EvidenceKind.OBSERVED, EvidenceKind.SOURCE_GROUNDED}
_REFUTATIONS = {
    Assessment.PRE_EXISTING,
    Assessment.INTENTIONAL,
    Assessment.UNREACHABLE,
    Assessment.DUPLICATE,
}


@dataclass(frozen=True, slots=True)
class ReviewPolicy:
    max_priority: int = 1
    min_confidence: float = 0.75
    max_added_production_lines: int = 20
    max_additional_production_files: int = 2
    allow_new_dependencies: bool = False
    allow_new_public_api: bool = False

    def _anchors_error(self, repo: Path, anchors: tuple[SourceAnchor, ...]) -> str | None:
        if not anchors:
            return "no source anchors were supplied"
        for anchor in anchors:
            try:
                path = safe_repo_path(repo, anchor.path)
            except ReviewReducerError as error:
                return str(error)
            if not path.is_file():
                return f"source anchor does not exist: {anchor.path}"
            try:
                line_count = len(path.read_text(encoding="utf-8").splitlines())
            except (OSError, UnicodeError) as error:
                return f"source anchor cannot be inspected: {anchor.path}: {error}"
            if not 1 <= anchor.line <= max(line_count, 1):
                return f"source anchor line is outside {anchor.path}: {anchor.line}"
        return None

    def decide(
        self,
        finding: Finding,
        challenge: Challenge,
        observation: Observation | None,
        snapshot: Snapshot,
    ) -> Decision:
        severe = finding.priority <= self.max_priority

        def result(verdict: Verdict, reason: str) -> Decision:
            return Decision(
                finding=finding,
                verdict=verdict,
                reason=reason,
                challenge=challenge,
                observation=observation,
                blocks_review=verdict in {Verdict.ACCEPT, Verdict.HUMAN_REVIEW},
                auto_fix_allowed=verdict is Verdict.ACCEPT,
            )

        def uncertain(reason: str) -> Decision:
            return result(
                Verdict.HUMAN_REVIEW if severe else Verdict.NON_BLOCKING,
                reason,
            )

        if challenge.finding_id != finding.finding_id:
            return uncertain("the adversary returned a different finding identifier")
        if observation and observation.finding_id != finding.finding_id:
            return uncertain("the blind verifier returned a different finding identifier")
        if not 0.0 <= challenge.confidence <= 1.0:
            return uncertain("adversarial confidence is outside the supported range")
        if observation and not 0.0 <= observation.confidence <= 1.0:
            return uncertain("blind-verifier confidence is outside the supported range")
        if not severe:
            return result(Verdict.NON_BLOCKING, "finding is below the configured priority")
        if challenge.assessment is Assessment.HUMAN_REQUIRED:
            return uncertain(challenge.rationale or "the finding needs human judgment")

        grounded = challenge.evidence_kind in _GROUNDED
        anchor_error = self._anchors_error(Path(snapshot.repo_root), challenge.source_anchors)
        valid_evidence = grounded and anchor_error is None

        if challenge.assessment in _REFUTATIONS:
            if not valid_evidence:
                return uncertain(anchor_error or "refutation lacks source-grounded evidence")
            return result(Verdict.REJECT, challenge.rationale)

        if challenge.assessment is Assessment.SPECULATIVE:
            if not valid_evidence:
                return uncertain(anchor_error or "the speculative-risk refutation is unproven")
            return result(Verdict.REJECT, challenge.rationale)

        if challenge.assessment is Assessment.NON_BLOCKING:
            if not valid_evidence:
                return uncertain(anchor_error or "the lower-severity classification is unproven")
            return result(Verdict.NON_BLOCKING, challenge.rationale)

        if challenge.assessment is Assessment.DISPROPORTIONATE:
            if not valid_evidence:
                return uncertain(anchor_error or "the proportionality judgment is unproven")
            return result(Verdict.HUMAN_REVIEW, challenge.rationale)

        if challenge.assessment is not Assessment.CONFIRMED:
            return uncertain("the adversary did not return a supported assessment")
        if not valid_evidence:
            return uncertain(anchor_error or "confirmation lacks source-grounded evidence")
        if challenge.reachable != "yes":
            return uncertain("the claimed failure has no proven reachable trigger")
        if challenge.changed_from_base not in {"yes", "newly_exposed"}:
            return uncertain("the behavior is not proven to be introduced by this change")
        if challenge.impact not in {"critical", "high", "moderate"}:
            return result(Verdict.NON_BLOCKING, "the confirmed impact is not consequential")
        if challenge.impact_evidence_kind not in _GROUNDED:
            return uncertain("real user impact lacks observed or source-grounded evidence")
        if not challenge.user_impact.strip():
            return uncertain("the confirmed finding has no source-grounded user impact")
        if challenge.confidence < self.min_confidence:
            return uncertain("the adversary could not establish sufficient confidence")
        if not challenge.realistic_trigger.strip():
            return uncertain("the confirmed finding has no concrete realistic trigger")
        if not challenge.smallest_fix.strip():
            return uncertain("no bounded direct fix was identified")
        if not challenge.preserves_change_intent:
            return uncertain("the proposed fix would reverse the pull request's intended change")
        if challenge.estimated_added_production_lines < 0:
            return uncertain("the estimated production-line count cannot be negative")
        if challenge.estimated_additional_production_files < 0:
            return uncertain("the estimated production-file count cannot be negative")
        if (
            challenge.estimated_added_production_lines
            > self.max_added_production_lines
        ):
            return uncertain("the smallest proposed fix exceeds the production-line budget")
        if (
            challenge.estimated_additional_production_files
            > self.max_additional_production_files
        ):
            return uncertain("the smallest proposed fix exceeds the production-file budget")
        if challenge.requires_new_dependency and not self.allow_new_dependencies:
            return uncertain("the proposed fix would introduce a dependency")
        if challenge.requires_new_public_api and not self.allow_new_public_api:
            return uncertain("the proposed fix would introduce a public API")

        if observation and observation.evidence_kind in _GROUNDED:
            observation_error = self._anchors_error(
                Path(snapshot.repo_root), observation.source_anchors
            )
            if observation_error:
                return uncertain(f"the blind observation is not verifiable: {observation_error}")
            if observation.changed_from_base == "no":
                return uncertain("the independent observer found pre-existing behavior")
            if observation.reachable == "no":
                return uncertain("the independent observer found no reachable failure")

        return result(Verdict.ACCEPT, challenge.rationale)

    def deduplicate(self, decisions: tuple[Decision, ...]) -> tuple[Decision, ...]:
        root_causes: set[str] = set()
        results: list[Decision] = []
        for decision in decisions:
            if decision.verdict is Verdict.ACCEPT and decision.challenge:
                semantic_id = decision.challenge.semantic_id
                if semantic_id in root_causes:
                    decision = Decision(
                        finding=decision.finding,
                        verdict=Verdict.REJECT,
                        reason="another accepted finding already covers this root cause",
                        challenge=decision.challenge,
                        observation=decision.observation,
                    )
                else:
                    root_causes.add(semantic_id)
            results.append(decision)
        return tuple(results)

    def enforce_repair_budget(self, churn: Churn) -> None:
        violations: list[str] = []
        if churn.production_added > self.max_added_production_lines:
            violations.append(
                f"added {churn.production_added} production lines "
                f"(maximum {self.max_added_production_lines})"
            )
        if len(churn.production_files) > self.max_additional_production_files:
            violations.append(
                f"changed {len(churn.production_files)} production files "
                f"(maximum {self.max_additional_production_files})"
            )
        if churn.dependency_files and not self.allow_new_dependencies:
            violations.append("changed dependency manifests: " + ", ".join(churn.dependency_files))
        if churn.public_api_additions and not self.allow_new_public_api:
            violations.append("added public API: " + ", ".join(churn.public_api_additions))
        if violations:
            raise BudgetExceededError(
                "automatic repair exceeded its hard complexity budget; "
                "the working-tree changes were preserved for inspection: "
                + "; ".join(violations)
            )
