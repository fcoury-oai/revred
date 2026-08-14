from __future__ import annotations

import io
import unittest
from unittest import mock

from review_reducer.display import ProgressDisplay, _duration, _tokens, _truncate
from review_reducer.models import Decision, Finding, Snapshot, Verdict


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


class ProgressDisplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = Snapshot(
            repo_root="/Users/felipe/code/example",
            base_ref="origin/main",
            base_sha="a" * 40,
            head_sha="b" * 40,
            merge_base_sha="a" * 40,
            patch_sha256="c" * 64,
            changed_files=("src/app.py",),
            dirty_paths=(),
            untracked_paths=(),
        )
        self.finding = Finding(
            title="[P1] Preserve caller contract",
            body="Existing callers reject the new value.",
            path="src/app.py",
            line_start=12,
            line_end=12,
            priority=1,
        )

    def display(self, **options: object) -> ProgressDisplay:
        return ProgressDisplay(mode="always", stream=_Terminal(), color=False, **options)

    def test_auto_enables_for_interactive_terminals(self) -> None:
        with mock.patch.dict("os.environ", {"TERM": "xterm-256color"}):
            dashboard = ProgressDisplay(mode="auto", stream=_Terminal())
        self.assertTrue(dashboard.enabled)

    def test_auto_disables_for_noninteractive_streams(self) -> None:
        dashboard = ProgressDisplay(mode="auto", stream=io.StringIO())
        self.assertFalse(dashboard.enabled)

    def test_auto_disables_for_dumb_terminals(self) -> None:
        with mock.patch.dict("os.environ", {"TERM": "dumb"}):
            dashboard = ProgressDisplay(mode="auto", stream=_Terminal())
        self.assertFalse(dashboard.enabled)

    def test_frame_shows_pipeline_repository_and_findings(self) -> None:
        dashboard = self.display()
        dashboard.configure(self.snapshot, "fix")
        dashboard.register_findings((self.finding,), phase="initial")
        frame = dashboard.render(width=100)
        self.assertIn("C O D E X   R E V I E W", frame)
        self.assertIn("PIPELINE", frame)
        self.assertIn("GUARDED REPAIR", frame)
        self.assertIn("origin/main", frame)
        self.assertIn("Preserve caller contract", frame)

    def test_agent_events_update_activity_usage_and_source_checks(self) -> None:
        dashboard = self.display()
        label = "initial-defense-123"
        dashboard.agent_event(label, {"type": "reducer.agent.started"})
        dashboard.agent_event(
            label,
            {
                "type": "item.started",
                "item": {"type": "command_execution", "command": "git show HEAD:app.py"},
            },
        )
        self.assertIn("git show", dashboard.render())
        dashboard.agent_event(
            label,
            {"type": "item.completed", "item": {"type": "command_execution"}},
        )
        dashboard.agent_event(
            label,
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 12_345,
                    "cached_input_tokens": 10_000,
                    "output_tokens": 321,
                },
            },
        )
        frame = dashboard.render()
        self.assertIn("12.3k in", frame)
        self.assertIn("10.0k cached", frame)
        self.assertIn("321 out", frame)
        self.assertIn("1 source checks", frame)

    def test_parallel_agent_names_include_the_finding_location(self) -> None:
        dashboard = self.display()
        dashboard.configure(self.snapshot, "review")
        dashboard.register_findings((self.finding,), phase="initial")
        label = f"initial-defense-{self.finding.finding_id}"
        dashboard.agent_event(label, {"type": "reducer.agent.started"})
        frame = dashboard.render(width=105)
        self.assertIn("adversarial review · src/app.py:12", frame)

    def test_model_commentary_is_shown_but_json_is_not(self) -> None:
        dashboard = self.display()
        label = "initial-blind-123"
        dashboard.agent_event(label, {"type": "reducer.agent.started"})
        dashboard.agent_event(
            label,
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Tracing the caller contract."},
            },
        )
        self.assertIn("Tracing the caller contract.", dashboard.render())
        dashboard.agent_event(
            label,
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"secret":"not displayed"}'},
            },
        )
        self.assertNotIn("not displayed", dashboard.render())

    def test_decisions_update_finding_counters(self) -> None:
        dashboard = self.display()
        dashboard.configure(self.snapshot, "review")
        dashboard.register_findings((self.finding,), phase="initial")
        dashboard.decision(
            Decision(finding=self.finding, verdict=Verdict.REJECT, reason="Already on base")
        )
        frame = dashboard.render()
        self.assertIn("1 rejected", frame)
        self.assertIn("[REJECTED]", frame)

    def test_unused_fix_stages_are_marked_skipped(self) -> None:
        dashboard = self.display()
        dashboard.configure(self.snapshot, "fix")
        dashboard.register_findings((), phase="initial")
        dashboard.finish("clean")
        frame = dashboard.render(width=100)
        self.assertIn("✓ review", frame)
        self.assertIn("✓ challenge", frame)
        self.assertIn("– repair", frame)
        self.assertIn("– final", frame)

    def test_context_restores_cursor_after_failure(self) -> None:
        stream = _Terminal()
        dashboard = ProgressDisplay(mode="always", stream=stream, color=False)
        with self.assertRaisesRegex(RuntimeError, "failure"):
            with dashboard:
                raise RuntimeError("failure")
        output = stream.getvalue()
        self.assertIn("\x1b[?25l", output)
        self.assertIn("\x1b[?25h", output)
        self.assertIn("FAILED", output)

    def test_no_color_preserves_layout_without_color_sequences(self) -> None:
        dashboard = self.display()
        dashboard.configure(self.snapshot, "review")
        frame = dashboard.render()
        self.assertIn("╭", frame)
        self.assertNotIn("\x1b[38;", frame)

    def test_noninteractive_mode_keeps_plain_progress(self) -> None:
        stream = io.StringIO()
        dashboard = ProgressDisplay(mode="auto", stream=stream)
        dashboard.note("reviewing pinned head abcdef")
        self.assertEqual(stream.getvalue(), "review-reducer: reviewing pinned head abcdef\n")
        self.assertNotIn("\x1b", stream.getvalue())

    def test_duration_tokens_and_unicode_truncation(self) -> None:
        self.assertEqual(_duration(65), "01:05")
        self.assertEqual(_tokens(12_345), "12.3k")
        self.assertEqual(_truncate("abcdef", 4), "abc…")
        self.assertLessEqual(len(_truncate("hello", 1)), 1)


if __name__ == "__main__":
    unittest.main()
