from __future__ import annotations

import unittest

from review_reducer.errors import SnapshotDriftError
from review_reducer.git import (
    capture_snapshot,
    classify_path,
    ensure_head,
    ensure_snapshot,
    measure_churn,
    working_tree_status,
)
from tests.support import GitFixture


class GitSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)

    def test_snapshot_pins_merge_base_and_original_churn(self) -> None:
        snapshot = capture_snapshot(self.fixture.repo, "main")
        self.assertEqual(snapshot.base_sha, snapshot.merge_base_sha)
        self.assertEqual(snapshot.changed_files, ("app.py",))
        self.assertEqual(snapshot.original_churn.production_added, 1)
        self.assertEqual(snapshot.original_churn.production_deleted, 1)
        self.assertFalse(snapshot.dirty_paths)

    def test_snapshot_distinguishes_dirty_and_untracked_paths(self) -> None:
        self.fixture.write("app.py", "def validate(value):\n    return 0\n")
        self.fixture.write("extra.py", "value = 1\n")
        snapshot = capture_snapshot(self.fixture.repo, "main")
        self.assertEqual(snapshot.dirty_paths, ("app.py",))
        self.assertEqual(snapshot.untracked_paths, ("extra.py",))
        self.assertEqual(working_tree_status(self.fixture.repo), (("app.py",), ("extra.py",)))

    def test_head_movement_is_rejected(self) -> None:
        snapshot = capture_snapshot(self.fixture.repo, "main")
        self.fixture.write("app.py", "def validate(value):\n    return 0\n")
        self.fixture.commit("move head")
        with self.assertRaises(SnapshotDriftError):
            ensure_head(snapshot)

    def test_read_only_working_tree_drift_is_rejected(self) -> None:
        snapshot = capture_snapshot(self.fixture.repo, "main")
        self.fixture.write("app.py", "def validate(value):\n    return 99\n")
        with self.assertRaisesRegex(SnapshotDriftError, "tracked working-tree patch"):
            ensure_snapshot(snapshot)

    def test_new_untracked_path_during_review_is_rejected(self) -> None:
        snapshot = capture_snapshot(self.fixture.repo, "main")
        self.fixture.write("new.py", "value = 1\n")
        with self.assertRaisesRegex(SnapshotDriftError, "untracked working-tree"):
            ensure_snapshot(snapshot)

    def test_classifies_tests_and_docs_separately(self) -> None:
        self.assertEqual(classify_path("src/handler.py"), "production")
        self.assertEqual(classify_path("tests/test_handler.py"), "test")
        self.assertEqual(classify_path("lib/handler.test.ts"), "test")
        self.assertEqual(classify_path("README.md"), "other")

    def test_detects_dependency_and_public_api_changes(self) -> None:
        self.fixture.write("Cargo.toml", "[package]\nname = 'example'\n")
        self.fixture.write("src/lib.rs", "pub fn exposed() {}\n")
        self.fixture.git("add", "Cargo.toml", "src/lib.rs")
        churn = measure_churn(self.fixture.repo, "HEAD")
        self.assertEqual(churn.dependency_files, ("Cargo.toml",))
        self.assertEqual(churn.public_api_additions, ("src/lib.rs: pub fn exposed() {}",))


if __name__ == "__main__":
    unittest.main()
