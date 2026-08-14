from __future__ import annotations

import json
import unittest

from review_reducer.errors import InvalidReviewError, ReviewReducerError
from review_reducer.parsing import parse_native_review
from tests.support import GitFixture


class NativeReviewParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)

    def test_parses_native_json_shape(self) -> None:
        payload = {
            "findings": [
                {
                    "title": "[P1] Clamp negative values",
                    "body": "Negative values become externally visible.",
                    "confidence_score": 0.93,
                    "priority": 1,
                    "code_location": {
                        "absolute_file_path": str(self.fixture.repo / "app.py"),
                        "line_range": {"start": 2, "end": 2},
                    },
                }
            ]
        }
        findings = parse_native_review(json.dumps(payload), self.fixture.repo)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, "app.py")
        self.assertEqual(findings[0].priority, 1)
        self.assertEqual(findings[0].confidence, 0.93)

    def test_parses_rendered_review_and_deduplicates(self) -> None:
        location = self.fixture.repo / "app.py"
        review = (
            "The change has a validation problem.\n\nFull review comments:\n\n"
            f"- [P1] Clamp negative values — {location}:2-2\n"
            "  Negative values become externally visible.\n\n"
            f"- [P1] Clamp negative values — {location}:2\n"
            "  The second copy is redundant.\n"
        )
        findings = parse_native_review(review, self.fixture.repo)
        self.assertEqual(len(findings), 1)
        self.assertIn("Negative values", findings[0].body)

    def test_unparseable_priority_tag_fails_closed(self) -> None:
        with self.assertRaisesRegex(InvalidReviewError, "could not be parsed"):
            parse_native_review("[P1] something serious without a location", self.fixture.repo)

    def test_paths_cannot_escape_repository(self) -> None:
        review = "- [P1] Read secrets — /etc/passwd:1-1\n  Not a repository file."
        with self.assertRaisesRegex(ReviewReducerError, "outside the reviewed repository"):
            parse_native_review(review, self.fixture.repo)

    def test_clean_prose_has_no_findings(self) -> None:
        self.assertEqual(parse_native_review("No actionable findings.", self.fixture.repo), [])


if __name__ == "__main__":
    unittest.main()
