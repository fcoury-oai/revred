"""Pinned Git snapshots and deliberately narrow repair measurements."""

from __future__ import annotations

from pathlib import Path
import hashlib
import re
import subprocess

from review_reducer.errors import ReviewReducerError, SnapshotDriftError
from review_reducer.models import Churn, Snapshot


_TEST_COMPONENTS = {"test", "tests", "__tests__", "fixtures", "snapshots"}
_DEPENDENCY_NAMES = {
    "cargo.lock",
    "cargo.toml",
    "composer.json",
    "composer.lock",
    "gemfile",
    "gemfile.lock",
    "go.mod",
    "go.sum",
    "package-lock.json",
    "package.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "requirements.txt",
    "uv.lock",
    "yarn.lock",
}
_DOC_SUFFIXES = {".md", ".mdx", ".rst", ".txt"}
_PUBLIC_API_ADDITION = re.compile(
    r"^\+\s*(?:pub(?:\([^)]*\))?\s+(?:(?:async|unsafe)\s+)*(?:fn|struct|enum|trait|type|const|static|mod)\b|export\s+(?:default\s+)?(?:async\s+)?(?:function|class|interface|type|const|let|enum)\b)"
)


def git(repo: Path, *args: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReviewReducerError(f"git {' '.join(args)} failed: {detail}")
    if binary:
        return result.stdout
    return result.stdout.decode("utf-8", errors="replace").strip()


def repository_root(path: Path) -> Path:
    return Path(str(git(path, "rev-parse", "--show-toplevel"))).resolve()


def git_common_dir(repo: Path) -> Path:
    common = Path(str(git(repo, "rev-parse", "--git-common-dir")))
    if not common.is_absolute():
        common = repo / common
    return common.resolve()


def _status(repo: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    output = git(repo, "status", "--porcelain=v1", "-z", binary=True)
    assert isinstance(output, bytes)
    dirty: list[str] = []
    untracked: list[str] = []
    records = output.decode("utf-8", errors="surrogateescape").split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        if not record:
            index += 1
            continue
        status, path = record[:2], record[3:]
        if status == "??":
            untracked.append(path)
        else:
            dirty.append(path)
            if "R" in status or "C" in status:
                index += 1
        index += 1
    return tuple(sorted(set(dirty))), tuple(sorted(set(untracked)))


def working_tree_status(repo: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Expose tracked and untracked paths without interpreting user content."""

    return _status(repo)


def classify_path(path: str) -> str:
    candidate = Path(path)
    pieces = {piece.lower() for piece in candidate.parts}
    name = candidate.name.lower()
    if (
        pieces & _TEST_COMPONENTS
        or name.startswith("test_")
        or name.endswith(("_test.py", "_test.rs", "_test.go", ".test.ts", ".spec.ts"))
        or candidate.suffix.lower() == ".snap"
    ):
        return "test"
    if candidate.suffix.lower() in _DOC_SUFFIXES:
        return "other"
    return "production"


def _public_api_additions(repo: Path, base: str) -> tuple[str, ...]:
    diff = str(git(repo, "diff", "--unified=0", base, "--"))
    current_path = ""
    matches: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            if _PUBLIC_API_ADDITION.match(line):
                matches.append(f"{current_path}: {line[1:].strip()}")
    return tuple(matches)


def measure_churn(repo: Path, base: str) -> Churn:
    output = str(git(repo, "diff", "--numstat", base, "--"))
    totals = {
        "production": [0, 0, []],
        "test": [0, 0, []],
        "other": [0, 0, []],
    }
    dependencies: list[str] = []
    for line in output.splitlines():
        added_text, deleted_text, path = line.split("\t", maxsplit=2)
        added = 0 if added_text == "-" else int(added_text)
        deleted = 0 if deleted_text == "-" else int(deleted_text)
        bucket = totals[classify_path(path)]
        bucket[0] += added
        bucket[1] += deleted
        bucket[2].append(path)
        if Path(path).name.lower() in _DEPENDENCY_NAMES:
            dependencies.append(path)
    return Churn(
        production_added=totals["production"][0],
        production_deleted=totals["production"][1],
        production_files=tuple(totals["production"][2]),
        test_added=totals["test"][0],
        test_deleted=totals["test"][1],
        test_files=tuple(totals["test"][2]),
        other_added=totals["other"][0],
        other_deleted=totals["other"][1],
        other_files=tuple(totals["other"][2]),
        dependency_files=tuple(dependencies),
        public_api_additions=_public_api_additions(repo, base),
    )


def capture_snapshot(path: Path, base_ref: str) -> Snapshot:
    repo = repository_root(path)
    head = str(git(repo, "rev-parse", "HEAD"))
    base = str(git(repo, "rev-parse", "--verify", f"{base_ref}^{{commit}}"))
    merge_base = str(git(repo, "merge-base", head, base))
    patch = git(repo, "diff", "--binary", merge_base, "--", binary=True)
    assert isinstance(patch, bytes)
    changed = str(git(repo, "diff", "--name-only", merge_base, "--"))
    dirty, untracked = _status(repo)
    return Snapshot(
        repo_root=str(repo),
        base_ref=base_ref,
        base_sha=base,
        head_sha=head,
        merge_base_sha=merge_base,
        patch_sha256=hashlib.sha256(patch).hexdigest(),
        changed_files=tuple(path for path in changed.splitlines() if path),
        dirty_paths=dirty,
        untracked_paths=untracked,
        original_churn=measure_churn(repo, merge_base),
    )


def ensure_head(snapshot: Snapshot) -> None:
    current = str(git(Path(snapshot.repo_root), "rev-parse", "HEAD"))
    if current != snapshot.head_sha:
        raise SnapshotDriftError(
            "the review head changed during the run: "
            f"expected {snapshot.head_sha}, found {current}"
        )


def patch_fingerprint(repo: Path, base: str) -> str:
    """Hash the exact tracked patch seen by base-targeted native review."""

    patch = git(repo, "diff", "--binary", base, "--", binary=True)
    assert isinstance(patch, bytes)
    return hashlib.sha256(patch).hexdigest()


def ensure_snapshot(
    snapshot: Snapshot,
    *,
    expected_patch: str | None = None,
    expected_untracked: tuple[str, ...] | None = None,
) -> None:
    """Reject concurrent branch or working-tree changes during model turns."""

    ensure_head(snapshot)
    repo = Path(snapshot.repo_root)
    actual_patch = patch_fingerprint(repo, snapshot.merge_base_sha)
    pinned_patch = snapshot.patch_sha256 if expected_patch is None else expected_patch
    if actual_patch != pinned_patch:
        raise SnapshotDriftError(
            "the tracked working-tree patch changed during a read-only review stage"
        )
    _, untracked = working_tree_status(repo)
    pinned_untracked = (
        snapshot.untracked_paths if expected_untracked is None else expected_untracked
    )
    if untracked != pinned_untracked:
        raise SnapshotDriftError("untracked working-tree paths changed during review")


def safe_repo_path(repo: Path, path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = repo / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(repo.resolve())
    except ValueError as error:
        raise ReviewReducerError(f"source path is outside the reviewed repository: {path}") from error
    return candidate
