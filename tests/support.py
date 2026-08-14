"""Small Git fixtures shared by behavior-focused tests."""

from __future__ import annotations

import json
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

    def github_pull_request(
        self,
        *,
        repository: str = "openai/codex",
        number: int = 123,
        fork: bool = False,
        **overrides: object,
    ) -> tuple[Path, dict[str, object]]:
        """Create deterministic GitHub CLI metadata without contacting GitHub."""

        self.git("remote", "add", "origin", f"https://github.com/{repository}.git")
        payload: dict[str, object] = {
            "number": number,
            "url": f"https://github.com/{repository}/pull/{number}",
            "title": "Preserve the validation floor",
            "baseRefName": "main",
            "baseRefOid": self.git("rev-parse", "main"),
            "headRefName": "feature",
            "headRefOid": self.git("rev-parse", "HEAD"),
            "isCrossRepository": fork,
            "headRepository": {"name": "codex-fork"} if fork else {"name": "codex"},
            "headRepositoryOwner": {"login": "contributor" if fork else "openai"},
        }
        payload.update(overrides)
        metadata = self.root / "github-pr.json"
        metadata.write_text(json.dumps(payload), encoding="utf-8")
        calls = self.root / "github-calls.jsonl"
        binary = self.root / "fake-gh"
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "from pathlib import Path\n"
            "import sys\n"
            f"with Path({str(calls)!r}).open('a', encoding='utf-8') as output:\n"
            "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            f"print(Path({str(metadata)!r}).read_text(encoding='utf-8'))\n",
            encoding="utf-8",
        )
        binary.chmod(0o755)
        return binary, payload
