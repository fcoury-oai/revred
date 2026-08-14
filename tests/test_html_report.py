from __future__ import annotations

from copy import deepcopy
from html.parser import HTMLParser
import io
import os
import unittest
from unittest import mock

from review_reducer.cli import _open_report
from review_reducer.git import capture_snapshot
from review_reducer.html_report import render_html_report
from tests.support import GitFixture


class _Elements(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


class HtmlReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        snapshot = capture_snapshot(self.fixture.repo, "main").to_dict()
        finding = {
            "finding_id": "abc123def456",
            "title": "[P2] Clamp negative validation output",
            "body": "The changed validation can return a negative value.",
            "path": "app.py",
            "line_start": 2,
            "line_end": 2,
            "priority": 2,
        }
        challenge = {
            "finding_id": finding["finding_id"],
            "assessment": "confirmed",
            "root_cause": "The changed validator subtracts before clamping.",
            "rationale": "Existing callers reject negative values.",
            "reachable": "yes",
            "changed_from_base": "yes",
            "impact": "moderate",
            "evidence_kind": "source_grounded",
            "source_anchors": [
                {"path": "app.py", "line": 2, "explanation": "changed return"}
            ],
            "realistic_trigger": "Call validate(0).",
            "user_impact": "Existing callers receive a negative value.",
            "impact_evidence_kind": "source_grounded",
            "smallest_fix": "Clamp the existing return expression.",
            "preserves_change_intent": True,
            "confidence": 0.97,
            "estimated_added_production_lines": 1,
            "estimated_additional_production_files": 1,
            "requires_new_dependency": False,
            "requires_new_public_api": False,
        }
        decision = {
            "finding": finding,
            "verdict": "accept",
            "reason": "Existing callers reject negative values.",
            "challenge": challenge,
            "adversarial_challenge": challenge,
            "reviewer_response": None,
            "observation": {
                "changed_behavior": "Validation subtracts one.",
                "source_anchors": challenge["source_anchors"],
                "user_impact": "Negative outputs reach callers.",
            },
            "auto_fix_allowed": True,
        }
        self.session = {
            "session_id": "20260814T160521.312582Z-abc123",
            "state": "complete",
            "status": "action_required",
            "snapshot": snapshot,
            "summary": {
                "total": 1,
                "accepted": 1,
                "rejected": 0,
                "non_blocking": 0,
                "human_review": 0,
                "resolved": 0,
            },
            "findings": [
                {
                    "finding": finding,
                    "decision": decision,
                    "investigations": {
                        "initial": {
                            "observation": decision["observation"],
                            "adversary": challenge,
                        }
                    },
                    "manual_override": None,
                    "resolved": False,
                }
            ],
            "usage": {
                "turn_count": 3,
                "input_tokens": 3250,
                "cached_input_tokens": 1100,
                "output_tokens": 640,
            },
        }
        self.report = {
            "status": "action_required",
            "snapshot": snapshot,
            "usage": self.session["usage"],
            "policy": {
                "max_added_production_lines": 20,
                "max_additional_production_files": 2,
            },
        }

    def test_report_contains_context_explanation_recommendation_and_evidence(self) -> None:
        document = render_html_report(self.report, self.session)
        self.assertIn("<!doctype html>", document)
        self.assertIn("The useful findings are ready.", document)
        self.assertIn("What is happening", document)
        self.assertIn("The changed validator subtracts before clamping.", document)
        self.assertIn("Why it matters", document)
        self.assertIn("Existing callers receive a negative value.", document)
        self.assertIn("Clamp the existing return expression.", document)
        self.assertIn("Blind investigation", document)
        self.assertIn("Adversarial assessment", document)
        self.assertIn("app.py:2", document)
        self.assertIn("3.2k", document)
        self.assertIn("1.1k cached", document)

    def test_document_is_offline_safe_and_escapes_untrusted_content(self) -> None:
        session = deepcopy(self.session)
        session["findings"][0]["finding"]["title"] = (
            '[P2] </h2><script>alert("owned")</script>'
        )
        session["findings"][0]["decision"]["challenge"]["user_impact"] = (
            '<img src="https://attacker.invalid/x" onerror="alert(1)">'
        )
        document = render_html_report(self.report, session)
        self.assertIn("&lt;script&gt;", document)
        self.assertIn("&lt;img", document)
        self.assertIn("default-src 'none'", document)
        self.assertNotIn("<script", document)
        elements = _Elements()
        elements.feed(document)
        self.assertFalse(any(tag in {"script", "iframe", "img"} for tag, _ in elements.elements))
        self.assertFalse(
            any(
                str(attrs.get("src", "")).startswith(("http:", "https:"))
                for _, attrs in elements.elements
            )
        )

    def test_human_review_explains_complexity_budget(self) -> None:
        session = deepcopy(self.session)
        session["status"] = "human_review_required"
        session["summary"]["accepted"] = 0
        session["summary"]["human_review"] = 1
        decision = session["findings"][0]["decision"]
        decision["verdict"] = "human_review"
        decision["reason"] = "the smallest proposed fix exceeds the production-line budget"
        decision["challenge"]["estimated_added_production_lines"] = 22
        document = render_html_report(self.report, session)
        self.assertIn("A decision is waiting for you.", document)
        self.assertIn("22 production lines", document)
        self.assertIn("20-line budget", document)
        self.assertIn("DECISION NEEDED", document)

    def test_manual_overrides_control_the_effective_report_decision(self) -> None:
        session = deepcopy(self.session)
        session["status"] = "clean"
        session["summary"]["accepted"] = 0
        session["summary"]["rejected"] = 1
        session["findings"][0]["manual_override"] = {
            "action": "dismiss",
            "reason": "Already protected by the parent branch.",
        }
        document = render_html_report(self.report, session)
        self.assertIn("Nothing is blocking this review.", document)
        self.assertIn("Already protected by the parent branch.", document)
        self.assertIn("DISMISSED", document)

    def test_sources_cannot_link_outside_the_reviewed_repository(self) -> None:
        session = deepcopy(self.session)
        session["findings"][0]["decision"]["challenge"]["source_anchors"].append(
            {"path": "../../../etc/passwd", "line": 1, "explanation": "untrusted"}
        )
        document = render_html_report(self.report, session)
        self.assertIn("../../../etc/passwd:1", document)
        self.assertNotIn('href="file:///etc/passwd', document)

    def test_empty_review_has_a_clean_state(self) -> None:
        session = deepcopy(self.session)
        session["status"] = "clean"
        session["summary"]["accepted"] = 0
        session["summary"]["total"] = 0
        session["findings"] = []
        document = render_html_report(self.report, session)
        self.assertIn("Nothing is blocking this review.", document)
        self.assertIn("No review findings were reported.", document)


class BrowserOpeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        self.path = self.fixture.root / "report.html"
        self.path.write_text("<!doctype html>", encoding="utf-8")

    def test_interactive_review_opens_local_html(self) -> None:
        with (
            mock.patch("review_reducer.cli.sys.stdout", _Terminal()),
            mock.patch("review_reducer.cli.sys.stderr", _Terminal()),
            mock.patch.dict(os.environ, {"CI": ""}),
            mock.patch("review_reducer.cli.webbrowser.open", return_value=True) as browser,
        ):
            _open_report(self.path, None)
        browser.assert_called_once_with(self.path.resolve().as_uri())

    def test_noninteractive_and_ci_reviews_do_not_open_automatically(self) -> None:
        with mock.patch("review_reducer.cli.webbrowser.open") as browser:
            _open_report(self.path, None)
        browser.assert_not_called()
        with (
            mock.patch("review_reducer.cli.sys.stdout", _Terminal()),
            mock.patch("review_reducer.cli.sys.stderr", _Terminal()),
            mock.patch.dict(os.environ, {"CI": "true"}),
            mock.patch("review_reducer.cli.webbrowser.open") as browser,
        ):
            _open_report(self.path, None)
        browser.assert_not_called()

    def test_explicit_open_overrides_noninteractive_detection(self) -> None:
        with mock.patch("review_reducer.cli.webbrowser.open", return_value=True) as browser:
            _open_report(self.path, True)
        browser.assert_called_once_with(self.path.resolve().as_uri())

    def test_no_open_report_disables_browser(self) -> None:
        with mock.patch("review_reducer.cli.webbrowser.open") as browser:
            _open_report(self.path, False)
        browser.assert_not_called()

    def test_browser_failure_does_not_fail_the_review(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch("review_reducer.cli.sys.stderr", stderr),
            mock.patch(
                "review_reducer.cli.webbrowser.open", side_effect=OSError("browser unavailable")
            ),
        ):
            _open_report(self.path, True)
        self.assertIn("could not open the HTML report", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
