"""A compact, dependency-free live dashboard for long Codex review runs."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import shutil
import sys
import threading
import time
from typing import Any, TextIO
import unicodedata

from review_reducer.models import Decision, Finding, Snapshot, Verdict


_SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_RESET = "\x1b[0m"
_DIM = "\x1b[38;5;244m"
_BRIGHT = "\x1b[1;38;5;255m"
_CYAN = "\x1b[38;5;87m"
_VIOLET = "\x1b[38;5;141m"
_GREEN = "\x1b[38;5;120m"
_AMBER = "\x1b[38;5;221m"
_RED = "\x1b[38;5;203m"


def _visible_width(value: str) -> int:
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"F", "W"}
        else 1
        for character in _ANSI.sub("", value)
    )


def _truncate(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if _visible_width(value) <= width:
        return value
    if width == 1:
        return "…"
    result: list[str] = []
    current = 0
    for character in value:
        size = _visible_width(character)
        if current + size > width - 1:
            break
        result.append(character)
        current += size
    return "".join(result) + "…"


def _clean(value: str, limit: int = 160) -> str:
    return _truncate(" ".join(_CONTROL.sub(" ", value).split()), limit)


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


@dataclass(slots=True)
class _Agent:
    label: str
    name: str
    started_at: float
    activity: str = "starting an isolated Codex turn"
    finished: bool = False
    failed: bool = False


@dataclass(slots=True)
class _Finding:
    finding_id: str
    title: str
    path: str
    line: int
    priority: int
    state: str = "queued"
    reason: str = ""


@dataclass(slots=True)
class _State:
    repo: str = ""
    base: str = ""
    head: str = ""
    mode: str = "review"
    status: str = "running"
    stage: str = "snapshot"
    note: str = "pinning repository state"
    started_at: float = field(default_factory=time.monotonic)
    agents: dict[str, _Agent] = field(default_factory=dict)
    findings: dict[str, _Finding] = field(default_factory=dict)
    turns: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    completed_stages: set[str] = field(default_factory=set)


class ProgressDisplay:
    """Render a live stderr dashboard while keeping stdout machine-readable."""

    def __init__(
        self,
        *,
        mode: str = "auto",
        stream: TextIO | None = None,
        refresh_seconds: float = 0.12,
        color: bool | None = None,
    ) -> None:
        self.stream = stream or sys.stderr
        terminal = bool(getattr(self.stream, "isatty", lambda: False)())
        self.enabled = mode == "always" or (
            mode == "auto" and terminal and os.environ.get("TERM") != "dumb"
        )
        self.color = not os.environ.get("NO_COLOR") if color is None else color
        self.refresh_seconds = refresh_seconds
        self.state = _State()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lines = 0
        self._frame = 0
        self._closed = False

    def __enter__(self) -> "ProgressDisplay":
        if self.enabled:
            self.stream.write("\x1b[?25l")
            self.stream.flush()
            self._thread = threading.Thread(
                target=self._refresh_loop,
                name="review-reducer-dashboard",
                daemon=True,
            )
            self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if exc_value is not None:
            self.finish("failed", str(exc_value))
        self.close()

    def _refresh_loop(self) -> None:
        while not self._stop.wait(self.refresh_seconds):
            self.draw()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self.enabled:
            self.draw()
            self.stream.write("\x1b[?25h\n")
            self.stream.flush()

    def configure(self, snapshot: Snapshot, mode: str) -> None:
        with self._lock:
            self.state.repo = snapshot.repo_root
            self.state.base = snapshot.base_ref
            self.state.head = snapshot.head_sha[:12]
            self.state.mode = mode
            self.state.stage = "review"
            self.state.note = "repository snapshot pinned"

    def _advance(self, stage: str) -> None:
        previous = self.state.stage
        if previous in {"review", "challenge", "repair", "final"} and previous != stage:
            self.state.completed_stages.add(previous)
        self.state.stage = stage

    def note(self, message: str, *, stage: str | None = None) -> None:
        with self._lock:
            if stage:
                self._advance(stage)
            self.state.note = _clean(message)
        if not self.enabled:
            print(f"review-reducer: {message}", file=self.stream, flush=True)

    def register_findings(self, findings: tuple[Finding, ...], *, phase: str) -> None:
        with self._lock:
            if phase == "initial":
                self.state.findings.clear()
            for finding in findings:
                self.state.findings[finding.finding_id] = _Finding(
                    finding_id=finding.finding_id,
                    title=re.sub(r"^\s*\[P[0-3]\]\s*", "", finding.title),
                    path=finding.path,
                    line=finding.line_start,
                    priority=finding.priority,
                )
            self._advance("final" if phase == "final" else "challenge")
            self.state.note = (
                f"{len(findings)} finding{'s' if len(findings) != 1 else ''} discovered"
            )

    def finding_step(self, finding: Finding, step: str) -> None:
        with self._lock:
            current = self.state.findings.get(finding.finding_id)
            if current:
                current.state = step
            if self.state.stage != "final":
                self._advance("challenge")

    def decision(self, decision: Decision) -> None:
        with self._lock:
            current = self.state.findings.get(decision.finding.finding_id)
            if current:
                current.state = {
                    Verdict.ACCEPT: "accepted",
                    Verdict.REJECT: "rejected",
                    Verdict.NON_BLOCKING: "non-blocking",
                    Verdict.HUMAN_REVIEW: "human review",
                }[decision.verdict]
                current.reason = _clean(decision.reason)

    def finish(self, status: str, note: str = "") -> None:
        with self._lock:
            if status != "failed" and self.state.stage in {
                "review", "challenge", "repair", "final"
            }:
                self.state.completed_stages.add(self.state.stage)
            self.state.status = status
            self.state.stage = "done" if status != "failed" else "failed"
            if note:
                self.state.note = _clean(note)

    def agent_event(self, label: str, event: dict[str, Any]) -> None:
        with self._lock:
            event_type = event.get("type", "")
            if event_type == "reducer.agent.started":
                self.state.agents[label] = _Agent(
                    label=label,
                    name=self._agent_name(label),
                    started_at=time.monotonic(),
                )
                return
            agent = self.state.agents.get(label)
            if not agent:
                return
            if event_type == "reducer.agent.finished":
                agent.finished = True
                return
            if event_type == "reducer.agent.failed":
                agent.finished = True
                agent.failed = True
                return
            if event_type == "turn.completed":
                usage = event.get("usage") or {}
                self.state.turns += 1
                self.state.input_tokens += self._usage_number(usage, "input_tokens")
                self.state.cached_tokens += self._usage_number(usage, "cached_input_tokens")
                self.state.output_tokens += self._usage_number(usage, "output_tokens")
                agent.finished = True
                agent.activity = "turn complete"
                return
            item = event.get("item") or {}
            item_type = item.get("type", "")
            if item_type == "command_execution":
                if event_type == "item.started":
                    agent.activity = "$ " + _clean(str(item.get("command", "")), 120)
                elif event_type == "item.completed":
                    self.state.tool_calls += 1
                    agent.activity = "inspected source and Git history"
                return
            if item_type == "agent_message" and event_type == "item.completed":
                text = str(item.get("text", ""))
                if text.strip() and not text.lstrip().startswith(("{", "[")):
                    agent.activity = _clean(text, 140)
                    self.state.note = agent.activity

    @staticmethod
    def _usage_number(usage: dict[str, Any], key: str) -> int:
        value = usage.get(key, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    def _agent_name(self, label: str) -> str:
        if label == "initial":
            return "native review"
        if label == "final":
            return "final review"
        if label == "repair":
            return "minimal repair"
        if label.startswith("followup-"):
            finding_id = label.split("-", maxsplit=2)[1]
        else:
            finding_id = label.rsplit("-", maxsplit=1)[-1]
        finding = self.state.findings.get(finding_id)
        location = f" · {finding.path}:{finding.line}" if finding else ""
        if label.startswith("followup-"):
            return "finding follow-up" + location
        if "-blind-" in label:
            return "blind investigator" + location
        if "-defense-" in label:
            return "adversarial review" + location
        if "-reviewer-" in label:
            return "reviewer rebuttal" + location
        return label

    def _style(self, value: str, color: str) -> str:
        return f"{color}{value}{_RESET}" if self.color else value

    def _line(self, value: str, width: int, color: str = "") -> str:
        visible = _truncate(value, width)
        padding = " " * max(0, width - _visible_width(visible))
        content = self._style(visible + padding, color) if color else visible + padding
        return self._style("│", _DIM) + " " + content + " " + self._style("│", _DIM)

    def _divider(self, left: str, right: str, width: int) -> str:
        return self._style(left + "─" * (width + 2) + right, _DIM)

    def _stage_line(self, spinner: str) -> str:
        stages = ("review", "challenge", "repair", "final")
        if self.state.mode != "fix":
            stages = ("review", "challenge")
        labels: list[str] = []
        for stage in stages:
            if stage in self.state.completed_stages:
                marker = "✓"
            elif self.state.stage == stage:
                marker = "×" if self.state.status == "failed" else spinner
            elif self.state.status != "running":
                marker = "–"
            else:
                marker = "○"
            labels.append(f"{marker} {stage}")
        return "   ".join(labels)

    def _finding_counts(self) -> str:
        states = [finding.state for finding in self.state.findings.values()]
        return (
            f"{len(states)} found   "
            f"{states.count('accepted')} accepted   "
            f"{states.count('rejected')} rejected   "
            f"{states.count('human review')} human"
        )

    def render(self, *, width: int | None = None) -> str:
        with self._lock:
            columns = width or shutil.get_terminal_size(fallback=(96, 24)).columns
            inner = max(24, min(columns - 4, 108))
            spinner = _SPINNER[self._frame % len(_SPINNER)]
            elapsed = _duration(time.monotonic() - self.state.started_at)
            status = {
                "running": f"{spinner} LIVE",
                "clean": "✓ CLEAN",
                "action_required": "! ACTION NEEDED",
                "human_review_required": "! HUMAN REVIEW",
                "failed": "× FAILED",
            }.get(self.state.status, self.state.status.upper())
            header = "◈  C O D E X   R E V I E W   R E D U C E R"
            meta = f"{status}  {elapsed}"
            heading = header + " " * max(1, inner - _visible_width(header) - len(meta)) + meta
            location = (
                f"{self.state.repo or 'locating repository'}   "
                f"{self.state.head or '···'} → {self.state.base or 'base'}"
            )
            mode = "GUARDED REPAIR" if self.state.mode == "fix" else "EVIDENCE REVIEW"
            lines = [
                self._divider("╭", "╮", inner),
                self._line(heading, inner, _VIOLET),
                self._line(location, inner, _DIM),
                self._line(mode, inner, _CYAN),
                self._divider("├", "┤", inner),
                self._line("PIPELINE", inner, _BRIGHT),
                self._line(self._stage_line(spinner), inner, _CYAN),
            ]

            active = [agent for agent in self.state.agents.values() if not agent.finished]
            recent = [agent for agent in self.state.agents.values() if agent.finished][-2:]
            if active or recent:
                lines.extend(
                    [
                        self._line("", inner),
                        self._line("ACTIVE AGENTS", inner, _BRIGHT),
                    ]
                )
                for agent in (active or recent)[-4:]:
                    marker = "×" if agent.failed else "✓" if agent.finished else spinner
                    runtime = _duration(time.monotonic() - agent.started_at)
                    title = f"{marker} {agent.name} · {runtime}"
                    lines.append(self._line(title, inner, _GREEN if agent.finished else _CYAN))
                    lines.append(self._line(f"  {agent.activity}", inner, _DIM))

            if self.state.findings:
                lines.extend(
                    [
                        self._line("", inner),
                        self._line("FINDINGS  " + self._finding_counts(), inner, _BRIGHT),
                    ]
                )
                for finding in list(self.state.findings.values())[:6]:
                    marker = {
                        "accepted": "!",
                        "rejected": "✓",
                        "human review": "?",
                        "non-blocking": "·",
                    }.get(finding.state, spinner if finding.state != "queued" else "○")
                    color = {
                        "accepted": _AMBER,
                        "rejected": _GREEN,
                        "human review": _RED,
                        "non-blocking": _DIM,
                    }.get(finding.state, _CYAN)
                    text = (
                        f"{marker} P{finding.priority} {finding.title}  "
                        f"[{finding.state.upper()}]"
                    )
                    lines.append(self._line(text, inner, color))
                remaining = len(self.state.findings) - 6
                if remaining > 0:
                    lines.append(self._line(f"  +{remaining} more findings", inner, _DIM))

            lines.extend(
                [
                    self._line("", inner),
                    self._line(f"LATEST  {self.state.note}", inner, _DIM),
                    self._divider("├", "┤", inner),
                    self._line(
                        f"{_tokens(self.state.input_tokens)} in  ·  "
                        f"{_tokens(self.state.cached_tokens)} cached  ·  "
                        f"{_tokens(self.state.output_tokens)} out  ·  "
                        f"{self.state.tool_calls} source checks  ·  "
                        f"{self.state.turns} turns  ·  "
                        f"{len(active)} active",
                        inner,
                        _CYAN,
                    ),
                    self._divider("╰", "╯", inner),
                ]
            )
            return "\n".join(lines)

    def draw(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            previous_lines = self._lines
            if previous_lines:
                self.stream.write(f"\x1b[{previous_lines}A")
            frame = self.render()
            rows = frame.splitlines()
            for row in rows:
                self.stream.write("\x1b[2K" + row + "\n")
            excess = max(0, previous_lines - len(rows))
            for _ in range(excess):
                self.stream.write("\x1b[2K\n")
            if excess:
                self.stream.write(f"\x1b[{excess}A")
            self.stream.flush()
            self._lines = len(rows)
            self._frame += 1
