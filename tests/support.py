"""Small Git fixtures shared by behavior-focused tests."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile


class GitFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="review-reducer-test-")
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "--initial-branch=main")
        self.write("app.py", "def validate(value):\n    return value\n")
        self.commit("initial source")
        self.git("checkout", "-q", "-b", "feature")
        self.write("app.py", "def validate(value):\n    return value - 1\n")
        self.commit("change validation")

    def cleanup(self) -> None:
        self.temporary.cleanup()

    def git(self, *args: str) -> str:
        environment = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        result = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            text=True,
            capture_output=True,
            check=True,
            env=environment,
        )
        return result.stdout.strip()

    def write(self, path: str, contents: str) -> Path:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
        return target

    def commit(self, message: str) -> None:
        self.git("add", "--all")
        self.git(
            "-c",
            "user.name=Review Reducer Test",
            "-c",
            "user.email=review-reducer@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            message,
        )
