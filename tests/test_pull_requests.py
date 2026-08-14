from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from review_reducer.errors import ReviewReducerError
from review_reducer.pull_requests import (
    _fetch_missing_commits,
    _new_worktree,
    parse_pull_request_reference,
    prepare_pull_request,
)
from tests.support import GitFixture


class PullRequestReferenceTests(unittest.TestCase):
    def test_accepts_number_repository_reference_and_github_url(self) -> None:
        examples = {
            "123": (None, 123),
            "#123": (None, 123),
            "openai/codex#123": ("openai/codex", 123),
            "https://github.com/openai/codex/pull/123": ("openai/codex", 123),
            "https://github.com/openai/codex/pull/123/": ("openai/codex", 123),
        }
        for reference, expected in examples.items():
            with self.subTest(reference=reference):
                self.assertEqual(parse_pull_request_reference(reference), expected)

    def test_rejects_invalid_repositories_numbers_and_hosts(self) -> None:
        references = (
            "0",
            "-1",
            "openai/codex#nope",
            "https://example.invalid/openai/codex/pull/1",
            "http://github.com/openai/codex/pull/1",
            "https://github.com/openai/codex/issues/1",
            "https://github.com/openai/codex/pull/1/more",
        )
        for reference in references:
            with self.subTest(reference=reference):
                with self.assertRaises(ReviewReducerError):
                    parse_pull_request_reference(reference)


class PullRequestPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        self.binary, self.payload = self.fixture.github_pull_request()

    def prepare(self, reference: str = "123", **options: object):
        values = {
            "repository": self.fixture.repo,
            "cwd": self.fixture.repo,
            "gh_binary": str(self.binary),
        }
        values.update(options)
        return prepare_pull_request(reference, **values)

    def test_reuses_clean_exact_head_checkout_and_pins_metadata(self) -> None:
        with mock.patch("review_reducer.pull_requests._new_worktree") as new_worktree:
            prepared = self.prepare()

        new_worktree.assert_not_called()
        self.assertEqual(prepared.checkout, self.fixture.repo.resolve())
        self.assertEqual(prepared.target.repository, "openai/codex")
        self.assertEqual(prepared.target.number, 123)
        self.assertEqual(prepared.target.base_sha, self.payload["baseRefOid"])
        self.assertEqual(prepared.target.head_sha, self.payload["headRefOid"])

    def test_secondary_github_remote_and_custom_ssh_user_are_supported(self) -> None:
        self.fixture.git("remote", "rename", "origin", "old-origin")
        self.fixture.git(
            "remote",
            "add",
            "origin",
            "org-14957082@github.com:openai/codex-internal.git",
        )

        prepared = self.prepare("openai/codex#123")

        self.assertEqual(prepared.target.repository, "openai/codex")
        self.assertEqual(prepared.checkout, self.fixture.repo.resolve())

    def test_fork_identity_does_not_prevent_exact_head_review(self) -> None:
        metadata = self.fixture.root / "github-pr.json"
        self.payload["isCrossRepository"] = True
        self.payload["headRepository"] = {"name": "forked-codex"}
        self.payload["headRepositoryOwner"] = {"login": "contributor"}
        metadata.write_text(json.dumps(self.payload), encoding="utf-8")
        prepared = self.prepare("openai/codex#123")

        self.assertTrue(prepared.target.is_fork)
        self.assertEqual(prepared.target.head_repository, "contributor/forked-codex")

    def test_stacked_pull_request_uses_its_actual_parent_base(self) -> None:
        metadata = self.fixture.root / "github-pr.json"
        self.payload["baseRefName"] = "feature/stack-parent"
        metadata.write_text(json.dumps(self.payload), encoding="utf-8")

        prepared = self.prepare()

        self.assertEqual(prepared.target.base_ref, "feature/stack-parent")
        self.assertEqual(prepared.target.base_sha, self.payload["baseRefOid"])

    def test_fork_with_deleted_head_repository_is_still_supported(self) -> None:
        metadata = self.fixture.root / "github-pr.json"
        self.payload["isCrossRepository"] = True
        self.payload["headRepository"] = None
        metadata.write_text(json.dumps(self.payload), encoding="utf-8")
        prepared = self.prepare()

        self.assertTrue(prepared.target.is_fork)
        self.assertIsNone(prepared.target.head_repository)

    def test_repository_slug_finds_existing_home_code_checkout(self) -> None:
        home = self.fixture.root / "fake-home"
        code = home / "code"
        code.mkdir(parents=True)
        (code / "codex").symlink_to(self.fixture.repo, target_is_directory=True)
        with mock.patch("review_reducer.pull_requests.Path.home", return_value=home):
            prepared = self.prepare(
                "openai/codex#123",
                repository="openai/codex",
                cwd=self.fixture.root,
            )

        self.assertEqual(prepared.checkout, self.fixture.repo.resolve())

    def test_rejects_repository_conflicts_before_contacting_github(self) -> None:
        with self.assertRaisesRegex(ReviewReducerError, "different GitHub repositories"):
            self.prepare("another/project#123", repository="openai/codex")

    def test_rejects_invalid_or_mismatched_github_metadata(self) -> None:
        failures = (
            ({"number": 124}, "different pull-request number"),
            ({"number": 123.0}, "different pull-request number"),
            ({"headRefOid": "not-a-commit"}, "invalid pull-request head SHA"),
            (
                {"url": "https://github.com/another/project/pull/123"},
                "different pull-request repository",
            ),
            ({"isCrossRepository": "false"}, "invalid pull-request fork indicator"),
        )
        metadata = self.fixture.root / "github-pr.json"
        for overrides, reason in failures:
            with self.subTest(overrides=overrides):
                metadata.write_text(json.dumps({**self.payload, **overrides}), encoding="utf-8")
                with self.assertRaisesRegex(ReviewReducerError, reason):
                    self.prepare()

    def test_rejects_local_checkout_with_mismatched_origin(self) -> None:
        with self.assertRaisesRegex(ReviewReducerError, "does not match checkout remotes"):
            self.prepare("another/project#123")

    def test_dirty_matching_checkout_requires_a_separate_worktree(self) -> None:
        self.fixture.write("app.py", "def validate(value):\n    return value + 4\n")
        with mock.patch(
            "review_reducer.pull_requests._new_worktree",
            return_value=self.fixture.repo,
        ) as new_worktree:
            self.prepare()

        new_worktree.assert_called_once()

    def test_base_branch_checkout_is_never_reused_for_pull_request_fixes(self) -> None:
        prepared = self.prepare()
        worktrees = [(self.fixture.repo, prepared.target.head_sha, "refs/heads/main")]
        with (
            mock.patch("review_reducer.pull_requests._worktrees", return_value=worktrees),
            mock.patch(
                "review_reducer.pull_requests._new_worktree",
                return_value=self.fixture.repo,
            ) as new_worktree,
        ):
            self.prepare()

        new_worktree.assert_called_once()

    def test_missing_local_repository_fails_without_cloning(self) -> None:
        with mock.patch("review_reducer.pull_requests.Path.home", return_value=self.fixture.root):
            with self.assertRaisesRegex(ReviewReducerError, "no local checkout"):
                self.prepare(
                    "different/repository#123",
                    repository="different/repository",
                    cwd=self.fixture.root,
                )

    def test_moving_head_during_fetch_is_rejected(self) -> None:
        prepared = self.prepare()
        target = replace(prepared.target, head_sha="f" * 40)
        with (
            mock.patch(
                "review_reducer.pull_requests._has_commit", side_effect=[True, False]
            ),
            mock.patch(
                "review_reducer.pull_requests._remote_for_repository", return_value="origin"
            ),
            mock.patch(
                "review_reducer.pull_requests.git",
                side_effect=["", "e" * 40],
            ),
        ):
            with self.assertRaisesRegex(ReviewReducerError, "head changed while fetching"):
                _fetch_missing_commits(self.fixture.repo, target)

    def test_missing_commits_are_fetched_using_exact_pr_and_base_refs(self) -> None:
        prepared = self.prepare()
        with (
            mock.patch(
                "review_reducer.pull_requests._has_commit", side_effect=[False, False]
            ),
            mock.patch(
                "review_reducer.pull_requests._remote_for_repository", return_value="origin"
            ),
            mock.patch(
                "review_reducer.pull_requests.git",
                side_effect=["", "", prepared.target.base_sha, prepared.target.head_sha],
            ) as run_git,
        ):
            _fetch_missing_commits(self.fixture.repo, prepared.target)

        fetch = run_git.call_args_list[1].args
        self.assertEqual(fetch[1:4], ("fetch", "--no-tags", "origin"))
        self.assertIn("+refs/heads/main:refs/review-reducer/pr/123/base", fetch)
        self.assertIn("+refs/pull/123/head:refs/review-reducer/pr/123/head", fetch)

    def test_new_worktree_is_durable_detached_and_disables_hooks(self) -> None:
        prepared = self.prepare()
        home = self.fixture.root / "durable-home"
        (home / "code").mkdir(parents=True)
        with (
            mock.patch("review_reducer.pull_requests.Path.home", return_value=home),
            mock.patch(
                "review_reducer.pull_requests.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as run,
            mock.patch(
                "review_reducer.pull_requests.git", return_value=prepared.target.head_sha
            ),
            mock.patch(
                "review_reducer.pull_requests.working_tree_status", return_value=((), ())
            ),
        ):
            destination = _new_worktree(
                self.fixture.repo,
                prepared.target,
                [(self.fixture.repo, prepared.target.head_sha, "refs/heads/feature")],
            )

        self.assertEqual(
            destination,
            home.resolve()
            / "code"
            / f"codex.review-pr-123-{prepared.target.head_sha[:12]}",
        )
        command = run.call_args.args[0]
        self.assertIn(f"core.hooksPath={os.devnull}", command)
        self.assertIn("--detach", command)
        self.assertIn(str(destination), command)


if __name__ == "__main__":
    unittest.main()
