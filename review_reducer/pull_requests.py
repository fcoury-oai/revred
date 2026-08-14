"""Resolve GitHub pull requests into exact, durable local review checkouts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any
from urllib.parse import urlsplit

from review_reducer.errors import ReviewReducerError
from review_reducer.git import git, repository_root, working_tree_status


_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")
_SSH_REMOTE = re.compile(r"(?:[^@/:]+@)?github\.com:(.+)")
_SHA = re.compile(r"[0-9a-fA-F]{40}")
_FIELDS = (
    "number,url,title,baseRefName,baseRefOid,headRefName,headRefOid,"
    "isCrossRepository,headRepository,headRepositoryOwner"
)


@dataclass(frozen=True, slots=True)
class PullRequestTarget:
    repository: str
    number: int
    url: str
    title: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    is_fork: bool
    head_repository: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Keep the exact reviewed GitHub identity in durable review artifacts."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class PreparedPullRequest:
    checkout: Path
    target: PullRequestTarget


def _slug(value: str) -> str | None:
    candidate = value.removesuffix(".git")
    return candidate if _REPOSITORY.fullmatch(candidate) else None


def parse_pull_request_reference(reference: str) -> tuple[str | None, int]:
    """Accept a number, owner/repo#number, or a github.com pull-request URL."""

    value = reference.strip()
    if value.startswith("https://") or value.startswith("http://"):
        parsed = urlsplit(value)
        parts = parsed.path.strip("/").split("/")
        if (
            parsed.scheme != "https"
            or parsed.netloc.lower() != "github.com"
            or len(parts) != 4
            or parts[2] != "pull"
        ):
            raise ReviewReducerError(f"unsupported GitHub pull-request URL: {reference!r}")
        repository = _slug("/".join(parts[:2]))
        number = parts[3]
    elif value.startswith("#"):
        repository = None
        number = value[1:]
    elif "#" in value:
        repository_part, separator, number = value.rpartition("#")
        repository = _slug(repository_part) if separator else None
    else:
        repository = None
        number = value.removeprefix("#")
    if (
        ("#" in value and not value.startswith("#")) or value.startswith("https://")
    ) and repository is None:
        raise ReviewReducerError(f"invalid GitHub repository in pull request: {reference!r}")
    if not number.isdecimal() or int(number) <= 0:
        raise ReviewReducerError(f"invalid GitHub pull-request number: {reference!r}")
    return repository, int(number)


def _remote_slug(remote: str) -> str | None:
    matched = _SSH_REMOTE.fullmatch(remote)
    if matched:
        value = matched.group(1)
    else:
        parsed = urlsplit(remote)
        if parsed.hostname != "github.com":
            return None
        value = parsed.path.strip("/")
    return _slug(value)


def _github_remotes(repo: Path) -> list[tuple[str, str]]:
    names = str(git(repo, "remote")).splitlines()
    ordered = sorted(
        (name.strip() for name in names if name.strip()),
        key=lambda name: name != "origin",
    )
    results: list[tuple[str, str]] = []
    for name in ordered:
        remote = str(git(repo, "remote", "get-url", name))
        repository = _remote_slug(remote)
        if repository:
            results.append((name, repository))
    return results


def _remote_repository(repo: Path) -> str:
    remotes = _github_remotes(repo)
    if not remotes:
        raise ReviewReducerError("the local repository has no GitHub remote")
    return remotes[0][1]


def _remote_for_repository(repo: Path, expected: str) -> str | None:
    for name, repository in _github_remotes(repo):
        if repository.casefold() == expected.casefold():
            return name
    return None


def _candidate_root(path: Path, expected: str) -> Path | None:
    if not path.is_dir():
        return None
    try:
        root = repository_root(path)
        remote = _remote_for_repository(root, expected)
    except ReviewReducerError:
        return None
    return root if remote is not None else None


def _find_checkout(repository: str, explicit: Path | None, cwd: Path) -> Path:
    if explicit is not None:
        try:
            root = repository_root(explicit.expanduser().resolve())
            remote = _remote_for_repository(root, repository)
            actual = _remote_repository(root)
        except ReviewReducerError as error:
            raise ReviewReducerError(
                f"cannot inspect the requested local repository {explicit}: {error}"
            ) from error
        if remote is None:
            raise ReviewReducerError(
                f"requested repository {repository} does not match checkout remotes "
                f"(origin: {actual})"
            )
        return root
    candidates = (
        cwd,
        Path.home() / "code" / repository.rsplit("/", maxsplit=1)[1],
        Path.home() / "code" / repository,
    )
    for candidate in candidates:
        root = _candidate_root(candidate, repository)
        if root is not None:
            return root
    raise ReviewReducerError(
        f"no local checkout of {repository} was found; "
        "pass --repo /path/to/the/repository"
    )


def _gh_pull_request(
    repository: str, number: int, checkout: Path, binary: str
) -> PullRequestTarget:
    command = [
        binary,
        "pr",
        "view",
        str(number),
        "--repo",
        repository,
        "--json",
        _FIELDS,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=checkout,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReviewReducerError(f"cannot inspect GitHub pull request: {error}") from error
    if result.returncode:
        detail = result.stderr.strip().splitlines()
        message = detail[-1] if detail else f"exit status {result.returncode}"
        raise ReviewReducerError(f"cannot inspect {repository}#{number}: {message}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ReviewReducerError("GitHub returned invalid pull-request metadata") from error
    if not isinstance(payload, dict):
        raise ReviewReducerError("GitHub pull-request metadata is not an object")
    try:
        returned_number = payload["number"]
        base_sha = payload["baseRefOid"]
        head_sha = payload["headRefOid"]
        base_ref = payload["baseRefName"]
        head_ref = payload["headRefName"]
        url = payload["url"]
        title = payload["title"]
        fork = payload["isCrossRepository"]
    except KeyError as error:
        raise ReviewReducerError(
            f"GitHub pull-request metadata omitted {error.args[0]}"
        ) from error
    if (
        not isinstance(returned_number, int)
        or isinstance(returned_number, bool)
        or returned_number != number
    ):
        raise ReviewReducerError("GitHub returned a different pull-request number")
    for name, value in (("base", base_sha), ("head", head_sha)):
        if not isinstance(value, str) or not _SHA.fullmatch(value):
            raise ReviewReducerError(f"GitHub returned an invalid pull-request {name} SHA")
    for name, value in (("base branch", base_ref), ("head branch", head_ref), ("URL", url)):
        if not isinstance(value, str) or not value:
            raise ReviewReducerError(f"GitHub returned an invalid pull-request {name}")
    returned_repository, returned_url_number = parse_pull_request_reference(url)
    if (
        returned_url_number != number
        or returned_repository is None
        or returned_repository.casefold() != repository.casefold()
    ):
        raise ReviewReducerError("GitHub returned a different pull-request repository")
    if not isinstance(fork, bool):
        raise ReviewReducerError("GitHub returned an invalid pull-request fork indicator")
    head_repository = payload.get("headRepository")
    head_owner = payload.get("headRepositoryOwner")
    head_name = head_repository.get("name") if isinstance(head_repository, dict) else None
    owner_name = head_owner.get("login") if isinstance(head_owner, dict) else None
    fork_repository = (
        f"{owner_name}/{head_name}"
        if isinstance(owner_name, str) and isinstance(head_name, str)
        else None
    )
    return PullRequestTarget(
        repository=returned_repository,
        number=number,
        url=url,
        title=title if isinstance(title, str) else "",
        base_ref=base_ref,
        base_sha=base_sha.lower(),
        head_ref=head_ref,
        head_sha=head_sha.lower(),
        is_fork=fork,
        head_repository=fork_repository,
    )


def _has_commit(checkout: Path, sha: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise ReviewReducerError(f"cannot inspect the local Git object database: {error}") from error
    return result.returncode == 0


def _fetch_missing_commits(checkout: Path, target: PullRequestTarget) -> None:
    missing_base = not _has_commit(checkout, target.base_sha)
    missing_head = not _has_commit(checkout, target.head_sha)
    if not missing_base and not missing_head:
        return
    remote = _remote_for_repository(checkout, target.repository)
    if remote is None:
        raise ReviewReducerError(
            f"the local checkout has no remote for {target.repository}"
        )
    specs: list[str] = []
    if missing_base:
        git(checkout, "check-ref-format", f"refs/heads/{target.base_ref}")
        specs.append(
            f"+refs/heads/{target.base_ref}:refs/review-reducer/pr/{target.number}/base"
        )
    if missing_head:
        specs.append(
            f"+refs/pull/{target.number}/head:refs/review-reducer/pr/{target.number}/head"
        )
    git(checkout, "fetch", "--no-tags", remote, *specs)
    if missing_base:
        fetched = str(
            git(checkout, "rev-parse", f"refs/review-reducer/pr/{target.number}/base")
        )
        if fetched != target.base_sha:
            raise ReviewReducerError(
                "the pull-request base changed while fetching; retry against its new exact commit"
            )
    if missing_head:
        fetched = str(
            git(checkout, "rev-parse", f"refs/review-reducer/pr/{target.number}/head")
        )
        if fetched != target.head_sha:
            raise ReviewReducerError(
                "the pull-request head changed while fetching; retry against its new exact commit"
            )


def _worktrees(checkout: Path) -> list[tuple[Path, str, str]]:
    output = str(git(checkout, "worktree", "list", "--porcelain"))
    result: list[tuple[Path, str, str]] = []
    for item in output.split("\n\n"):
        path: Path | None = None
        head = ""
        branch = ""
        for line in item.splitlines():
            if line.startswith("worktree "):
                path = Path(line.removeprefix("worktree ")).resolve()
            elif line.startswith("HEAD "):
                head = line.removeprefix("HEAD ").strip()
            elif line.startswith("branch "):
                branch = line.removeprefix("branch ").strip()
        if path is not None and head:
            result.append((path, head, branch))
    return result


def _clean_matching_worktree(
    checkout: Path, target: PullRequestTarget, worktrees: list[tuple[Path, str, str]]
) -> Path | None:
    preferred = checkout.resolve()
    ordered = sorted(worktrees, key=lambda item: item[0] != preferred)
    for path, head, branch in ordered:
        if head != target.head_sha or not path.is_dir():
            continue
        if branch == f"refs/heads/{target.base_ref}":
            continue
        dirty, untracked = working_tree_status(path)
        if not dirty and not untracked:
            return path
    return None


def _new_worktree(
    checkout: Path, target: PullRequestTarget, worktrees: list[tuple[Path, str, str]]
) -> Path:
    durable_root = (Path.home() / "code").resolve()
    if not durable_root.is_dir():
        raise ReviewReducerError(
            f"durable worktree directory does not exist: {durable_root}"
        )
    primary = worktrees[0][0] if worktrees else checkout
    name = (
        primary.name
        if primary.parent == durable_root
        else target.repository.rsplit("/", 1)[1]
    )
    destination = durable_root / f"{name}.review-pr-{target.number}-{target.head_sha[:12]}"
    if destination.exists():
        raise ReviewReducerError(
            f"the durable PR worktree path already exists and cannot be reused: {destination}"
        )
    command = [
        "git",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-C",
        str(checkout),
        "worktree",
        "add",
        "--detach",
        str(destination),
        target.head_sha,
    ]
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
    except OSError as error:
        raise ReviewReducerError(f"cannot create the durable PR worktree: {error}") from error
    if result.returncode:
        detail = result.stderr.strip().splitlines()
        message = detail[-1] if detail else f"exit status {result.returncode}"
        raise ReviewReducerError(f"cannot create the durable PR worktree: {message}")
    actual = str(git(destination, "rev-parse", "HEAD"))
    if actual != target.head_sha:
        raise ReviewReducerError("the durable PR worktree does not match the exact GitHub head")
    dirty, untracked = working_tree_status(destination)
    if dirty or untracked:
        raise ReviewReducerError("the new durable PR worktree is unexpectedly dirty")
    return destination


def prepare_pull_request(
    reference: str,
    *,
    repository: Path | str | None = None,
    cwd: Path | None = None,
    gh_binary: str = "gh",
) -> PreparedPullRequest:
    """Pin a PR's live GitHub head/base and return a safe durable local checkout."""

    current = (cwd or Path.cwd()).resolve()
    requested_repository, number = parse_pull_request_reference(reference)
    repository_text = str(repository) if repository is not None else ""
    repository_hint = _slug(repository_text) if repository_text else None
    explicit_path = None if repository is None or repository_hint else Path(repository_text)
    if requested_repository and repository_hint:
        if requested_repository.casefold() != repository_hint.casefold():
            raise ReviewReducerError("--repo and --pr identify different GitHub repositories")
    if requested_repository:
        selected_repository = requested_repository
    elif repository_hint:
        selected_repository = repository_hint
    else:
        candidate = explicit_path.expanduser().resolve() if explicit_path else current
        try:
            selected_repository = _remote_repository(repository_root(candidate))
        except ReviewReducerError as error:
            raise ReviewReducerError(
                "a numeric --pr requires --repo owner/name or a local GitHub checkout"
            ) from error
    checkout = _find_checkout(selected_repository, explicit_path, current)
    target = _gh_pull_request(selected_repository, number, checkout, gh_binary)
    _fetch_missing_commits(checkout, target)
    existing = _worktrees(checkout)
    selected = _clean_matching_worktree(checkout, target, existing)
    if selected is None:
        selected = _new_worktree(checkout, target, existing)
    if str(git(selected, "rev-parse", "HEAD")) != target.head_sha:
        raise ReviewReducerError("the selected PR checkout no longer matches the GitHub head")
    return PreparedPullRequest(checkout=selected, target=target)
