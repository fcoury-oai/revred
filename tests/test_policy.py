from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from review_reducer.codex import CodexRunner, validate_schema
from review_reducer.errors import BudgetExceededError, InvalidReviewError
from review_reducer.git import capture_snapshot
from review_reducer.models import (
    Assessment,
    Challenge,
    Churn,
    Decision,
    EvidenceKind,
    Finding,
    Observation,
    SourceAnchor,
    Verdict,
)
from review_reducer.policy import ReviewPolicy
from tests.support import GitFixture


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        self.snapshot = capture_snapshot(self.fixture.repo, "main")
        self.finding = Finding(
            title="[P1] Clamp negative values",
            body="Negative values are returned to callers.",
            path="app.py",
            line_start=2,
            line_end=2,
            priority=1,
        )
        anchor = SourceAnchor(path="app.py", line=2, explanation="changed return value")
        self.challenge = Challenge(
            finding_id=self.finding.finding_id,
            assessment=Assessment.CONFIRMED,
            root_cause="negative output from changed validation",
            rationale="The changed return is reachable from public callers.",
            reachable="yes",
            changed_from_base="yes",
            impact="high",
            evidence_kind=EvidenceKind.SOURCE_GROUNDED,
            source_anchors=(anchor,),
            realistic_trigger="validate(0)",
            user_impact="Existing callers reject negative outputs.",
            impact_evidence_kind=EvidenceKind.SOURCE_GROUNDED,
            smallest_fix="Clamp the existing return expression.",
            preserves_change_intent=True,
            confidence=0.95,
            estimated_added_production_lines=1,
            estimated_additional_production_files=1,
            requires_new_dependency=False,
            requires_new_public_api=False,
        )
        self.observation = Observation(
            finding_id=self.finding.finding_id,
            changed_behavior="Validation subtracts one.",
            reachable="yes",
            changed_from_base="yes",
            evidence_kind=EvidenceKind.SOURCE_GROUNDED,
            source_anchors=(anchor,),
            realistic_trigger="validate(0)",
            user_impact="Callers receive negative values.",
            confidence=0.9,
            uncertainties=(),
        )
        self.policy = ReviewPolicy()

    def decide(self, challenge: Challenge | None = None) -> Decision:
        return self.policy.decide(
            self.finding,
            challenge or self.challenge,
            self.observation,
            self.snapshot,
        )

    def test_accepts_grounded_reachable_introduced_failure(self) -> None:
        decision = self.decide()
        self.assertEqual(decision.verdict, Verdict.ACCEPT)
        self.assertTrue(decision.blocks_review)
        self.assertTrue(decision.auto_fix_allowed)

    def test_priority_never_suppresses_confirmed_or_uncertain_findings(self) -> None:
        for priority in (2, 3):
            with self.subTest(priority=priority):
                finding = replace(self.finding, priority=priority)
                accepted = self.policy.decide(
                    finding, self.challenge, self.observation, self.snapshot
                )
                self.assertEqual(accepted.verdict, Verdict.ACCEPT)
                uncertain = self.policy.decide(
                    finding,
                    replace(
                        self.challenge,
                        assessment=Assessment.PRE_EXISTING,
                        evidence_kind=EvidenceKind.HYPOTHETICAL,
                        source_anchors=(),
                    ),
                    self.observation,
                    self.snapshot,
                )
                self.assertEqual(uncertain.verdict, Verdict.HUMAN_REVIEW)

    def test_rejects_source_proven_inherited_behavior(self) -> None:
        decision = self.decide(
            replace(self.challenge, assessment=Assessment.PRE_EXISTING, changed_from_base="no")
        )
        self.assertEqual(decision.verdict, Verdict.REJECT)
        self.assertFalse(decision.blocks_review)

    def test_uncorroborated_severe_refutation_needs_human(self) -> None:
        decision = self.decide(
            replace(
                self.challenge,
                assessment=Assessment.PRE_EXISTING,
                evidence_kind=EvidenceKind.HYPOTHETICAL,
                source_anchors=(),
            )
        )
        self.assertEqual(decision.verdict, Verdict.HUMAN_REVIEW)

    def test_missing_or_external_anchor_needs_human(self) -> None:
        decision = self.decide(replace(self.challenge, source_anchors=()))
        self.assertEqual(decision.verdict, Verdict.HUMAN_REVIEW)
        external = replace(
            self.challenge,
            source_anchors=(SourceAnchor(path="/etc/passwd", line=1),),
        )
        self.assertEqual(self.decide(external).verdict, Verdict.HUMAN_REVIEW)

    def test_observer_contradiction_needs_human(self) -> None:
        observation = replace(self.observation, changed_from_base="no")
        decision = self.policy.decide(
            self.finding, self.challenge, observation, self.snapshot
        )
        self.assertEqual(decision.verdict, Verdict.HUMAN_REVIEW)

    def test_low_impact_is_non_blocking(self) -> None:
        decision = self.decide(replace(self.challenge, impact="low"))
        self.assertEqual(decision.verdict, Verdict.NON_BLOCKING)

    def test_excessive_proposed_fix_needs_human(self) -> None:
        decision = self.decide(replace(self.challenge, estimated_added_production_lines=21))
        self.assertEqual(decision.verdict, Verdict.HUMAN_REVIEW)

    def test_inferred_user_harm_needs_human(self) -> None:
        decision = self.decide(
            replace(self.challenge, impact_evidence_kind=EvidenceKind.INFERRED)
        )
        self.assertEqual(decision.verdict, Verdict.HUMAN_REVIEW)

    def test_fix_reversing_intended_behavior_needs_human(self) -> None:
        decision = self.decide(replace(self.challenge, preserves_change_intent=False))
        self.assertEqual(decision.verdict, Verdict.HUMAN_REVIEW)

    def test_duplicate_root_causes_are_collapsed(self) -> None:
        first = self.decide()
        second_finding = replace(self.finding, title="[P1] Reject invalid validation output")
        second_challenge = replace(
            self.challenge, finding_id=second_finding.finding_id
        )
        second = self.policy.decide(
            second_finding, second_challenge, None, self.snapshot
        )
        decisions = self.policy.deduplicate((first, second))
        self.assertEqual(decisions[0].verdict, Verdict.ACCEPT)
        self.assertEqual(decisions[1].verdict, Verdict.REJECT)

    def test_repair_churn_budget_is_enforced(self) -> None:
        with self.assertRaises(BudgetExceededError):
            self.policy.enforce_repair_budget(
                Churn(production_added=21, production_files=("app.py",))
            )
        with self.assertRaises(BudgetExceededError):
            self.policy.enforce_repair_budget(
                Churn(production_files=("a.py", "b.py", "c.py"))
            )
        with self.assertRaises(BudgetExceededError):
            self.policy.enforce_repair_budget(
                Churn(dependency_files=("Cargo.toml",))
            )

    def test_schema_rejects_boolean_as_integer(self) -> None:
        with self.assertRaises(InvalidReviewError):
            validate_schema(True, {"type": "integer"})

    def test_usage_summarizes_actual_completed_turns(self) -> None:
        with tempfile.TemporaryDirectory(prefix="review-reducer-usage-") as temporary:
            directory = Path(temporary)
            event = {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 42,
                    "cached_input_tokens": 11,
                    "output_tokens": 7,
                },
            }
            (directory / "defense-test.events.jsonl").write_text(
                json.dumps(event) + "\n", encoding="utf-8"
            )
            runner = CodexRunner(repo=self.fixture.repo, run_dir=directory)
            usage = runner.usage_summary()
            self.assertEqual(usage["turn_count"], 1)
            self.assertEqual(usage["input_tokens"], 42)
            self.assertEqual(usage["cached_input_tokens"], 11)
            self.assertEqual(usage["output_tokens"], 7)
            self.assertEqual(usage["turns"][0]["role"], "defense-test")


if __name__ == "__main__":
    unittest.main()
