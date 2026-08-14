from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from review_reducer.cli import main
from review_reducer.errors import BudgetExceededError, ReviewReducerError
from review_reducer.policy import ReviewPolicy
from review_reducer.pull_requests import prepare_pull_request
from review_reducer.sessions import ReviewSession, list_sessions
from review_reducer.workflow import RunConfig, ReviewWorkflow
from tests.support import GitFixture


_FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import re
import sys
import time

argv = sys.argv[1:]
state_path = Path(os.environ["FAKE_CODEX_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else []
native = "review" in argv
schema = None
if "--output-schema" in argv:
    schema = Path(argv[argv.index("--output-schema") + 1]).name
state.append({"native": native, "schema": schema, "argv": argv})
state_path.write_text(json.dumps(state))
response_path = Path(argv[argv.index("--output-last-message") + 1])
prompt = sys.stdin.read()
match = re.search(r'"finding_id"\s*:\s*"([^"]+)"', prompt)
finding_id = match.group(1) if match else ""
anchor = {"path": "app.py", "line": 2, "explanation": "changed return"}

if native:
    count = sum(item["native"] for item in state)
    priority = os.environ.get("FAKE_PRIORITY", "1")
    response = (
        "No actionable findings."
        if count > 1
        else f"Full review comments:\n\n- [P{priority}] Clamp negative values — "
             f"{Path.cwd() / 'app.py'}:2-2\n  Negative values escape validation.\n"
    )
    if count == 1 and os.environ.get("FAKE_SECOND_FINDING"):
        response += (
            f"\n- [P{priority}] Preserve the validation floor — "
            f"{Path.cwd() / 'app.py'}:2-2\n  Duplicate negative-value concern.\n"
        )
elif schema == "observation.json":
    response = json.dumps({
        "finding_id": finding_id,
        "changed_behavior": "Validation subtracts one.",
        "reachable": "yes",
        "changed_from_base": "yes",
        "evidence_kind": "source_grounded",
        "source_anchors": [anchor],
        "realistic_trigger": "validate(0)",
        "user_impact": "Negative outputs reach callers.",
        "confidence": 0.94,
        "uncertainties": [],
    })
elif schema == "challenge.json":
    reviewer = "original code reviewer's evidence-grounded advocate" in prompt
    assessment = os.environ.get(
        "FAKE_REVIEWER_ASSESSMENT" if reviewer else "FAKE_ASSESSMENT",
        os.environ.get("FAKE_ASSESSMENT", "confirmed"),
    )
    evidence = os.environ.get("FAKE_EVIDENCE", "source_grounded")
    payload = {
        "finding_id": finding_id,
        "assessment": assessment,
        "root_cause": "negative output from changed validation",
        "rationale": "The changed return expression reaches existing callers.",
        "reachable": "yes",
        "changed_from_base": "no" if assessment == "pre_existing" else "yes",
        "impact": os.environ.get("FAKE_IMPACT", "high"),
        "evidence_kind": evidence,
        "source_anchors": [anchor] if evidence == "source_grounded" else [],
        "realistic_trigger": "validate(0)",
        "user_impact": "A documented caller rejects negative values.",
        "impact_evidence_kind": "source_grounded",
        "smallest_fix": "Clamp the existing return expression.",
        "preserves_change_intent": True,
        "confidence": 0.97,
        "estimated_added_production_lines": 1,
        "estimated_additional_production_files": 1,
        "requires_new_dependency": False,
        "requires_new_public_api": False,
    }
    if os.environ.get("FAKE_INVALID_SCHEMA"):
        payload["requires_new_dependency"] = "false"
    response = json.dumps(payload)
elif schema == "followup.json":
    source = dict(anchor)
    anchor_mode = os.environ.get("FAKE_FOLLOWUP_ANCHOR", "valid")
    if anchor_mode == "outside":
        source["path"] = "/etc/passwd"
    elif anchor_mode == "missing":
        source["path"] = "missing.py"
    elif anchor_mode == "bad_line":
        source["line"] = 999
    response = json.dumps({
        "finding_id": (
            "not-the-same-finding" if os.environ.get("FAKE_FOLLOWUP_WRONG_ID") else finding_id
        ),
        "answer_status": os.environ.get("FAKE_FOLLOWUP_STATUS", "answered"),
        "answer": os.environ.get(
            "FAKE_FOLLOWUP_ANSWER",
            "Yes. The changed validation reaches an existing caller, and a one-line clamp preserves its intent.",
        ),
        "evidence_kind": os.environ.get("FAKE_FOLLOWUP_EVIDENCE", "source_grounded"),
        "source_anchors": [] if anchor_mode == "none" else [source],
        "confidence": float(os.environ.get("FAKE_FOLLOWUP_CONFIDENCE", "0.96")),
        "suggested_verdict": os.environ.get("FAKE_FOLLOWUP_VERDICT", "unchanged"),
        "recommended_action": "Clamp the existing return expression.",
        "smallest_fix": "return max(value - 1, 0)",
        "estimated_added_production_lines": int(os.environ.get("FAKE_FOLLOWUP_LINES", "1")),
        "uncertainties": [],
    })
    if os.environ.get("FAKE_FOLLOWUP_INVALID_SCHEMA"):
        payload = json.loads(response)
        del payload["answer"]
        response = json.dumps(payload)
    if os.environ.get("FAKE_FOLLOWUP_DRIFT"):
        Path("app.py").write_text("def validate(value):\n    return value + 4\n")
elif schema == "fix.json":
    mode = os.environ.get("FAKE_REPAIR", "normal")
    source = Path("app.py")
    content = source.read_text().replace(
        "return value - 1", "return max(value - 1, 0)"
    )
    if mode == "budget":
        content += "".join(f"# unnecessary line {index}\n" for index in range(30))
    source.write_text(content)
    if mode == "untracked":
        Path("untracked.py").write_text("new = True\n")
    response = json.dumps({
        "summary": "Clamp the existing validation return.",
        "applied_finding_ids": [finding_id],
        "changed_files": ["app.py"],
        "checks_run": [],
        "remaining_risks": [],
    })
else:
    raise SystemExit(f"unsupported fake invocation: {argv}")

response_path.write_text(response)
print(json.dumps({
    "type": "item.started",
    "item": {"type": "command_execution", "command": "git diff -- app.py"},
}), flush=True)
if os.environ.get("FAKE_CODEX_DELAY"):
    time.sleep(float(os.environ["FAKE_CODEX_DELAY"]))
print(json.dumps({
    "type": "item.completed",
    "item": {"type": "command_execution", "command": "git diff -- app.py"},
}), flush=True)
print(json.dumps({
    "type": "item.completed",
    "item": {"type": "agent_message", "text": "Inspecting the exact changed caller contract."},
}), flush=True)
print(json.dumps({"type": "turn.completed"}), flush=True)
'''


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        self.binary = self.fixture.root / "fake-codex"
        self.binary.write_text(_FAKE_CODEX, encoding="utf-8")
        self.binary.chmod(0o755)
        self.state = self.fixture.root / "state.json"
        self.environment = mock.patch.dict(
            os.environ,
            {"FAKE_CODEX_STATE": str(self.state)},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def config(self, **overrides: object) -> RunConfig:
        values = {
            "repo": self.fixture.repo,
            "base": "main",
            "codex_bin": str(self.binary),
            "jobs": 1,
        }
        values.update(overrides)
        return RunConfig(**values)

    def calls(self) -> list[dict[str, object]]:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def test_report_only_keeps_verified_finding_without_editing(self) -> None:
        report = ReviewWorkflow(self.config()).run()
        self.assertEqual(report["status"], "action_required")
        self.assertEqual(report["initial_decisions"][0]["verdict"], "accept")
        self.assertIsNone(report["repair"])
        self.assertEqual([call["schema"] for call in self.calls()],
                         [None, "observation.json", "challenge.json"])
        self.assertIn("return value - 1", (self.fixture.repo / "app.py").read_text())
        self.assertTrue(Path(report["artifacts_dir"], "report.json").is_file())
        self.assertTrue(Path(report["html_report"]).is_file())
        self.assertIn("FIX RECOMMENDED", Path(report["html_report"]).read_text())
        self.assertEqual(report["policy"]["max_added_production_lines"], 20)

    def test_github_pull_request_review_pins_head_base_and_saved_identity(self) -> None:
        gh, metadata = self.fixture.github_pull_request(number=321, fork=True)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main([
                "review", "--repo", str(self.fixture.repo), "--pr", "321",
                "--gh-bin", str(gh), "--codex-bin", str(self.binary),
                "--jobs", "1", "--json", "--no-open-report",
            ])

        self.assertEqual(code, 2)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["pull_request"]["repository"], "openai/codex")
        self.assertEqual(report["pull_request"]["number"], 321)
        self.assertTrue(report["pull_request"]["is_fork"])
        self.assertEqual(report["snapshot"]["head_sha"], metadata["headRefOid"])
        self.assertEqual(report["snapshot"]["base_sha"], metadata["baseRefOid"])
        session = ReviewSession.open(Path(report["artifacts_dir"]))
        self.assertEqual(session.data["pull_request"]["number"], 321)
        self.assertIn("openai/codex#321", Path(report["html_report"]).read_text())
        self.assertIn(str(metadata["baseRefOid"]), self.calls()[0]["argv"])

    def test_github_pull_request_review_rejects_checkout_drift(self) -> None:
        gh, metadata = self.fixture.github_pull_request(number=321)
        prepared = prepare_pull_request(
            "321", repository=self.fixture.repo, gh_binary=str(gh)
        )
        self.fixture.write("app.py", "def validate(value):\n    return value - 4\n")
        self.fixture.commit("move pull request head")

        with self.assertRaisesRegex(ReviewReducerError, "exact GitHub PR head"):
            ReviewWorkflow(
                self.config(
                    base=str(metadata["baseRefOid"]),
                    pull_request=prepared.target,
                )
            ).run()

    def test_github_pull_request_review_rejects_dirty_checkout(self) -> None:
        gh, metadata = self.fixture.github_pull_request(number=321)
        prepared = prepare_pull_request(
            "321", repository=self.fixture.repo, gh_binary=str(gh)
        )
        self.fixture.write("app.py", "def validate(value):\n    return value + 9\n")

        with self.assertRaisesRegex(ReviewReducerError, "clean exact-head worktree"):
            ReviewWorkflow(
                self.config(
                    base=str(metadata["baseRefOid"]),
                    pull_request=prepared.target,
                )
            ).run()

    def test_preexisting_finding_is_rejected_without_repair(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_ASSESSMENT": "pre_existing"}):
            report = ReviewWorkflow(self.config(mode="fix")).run()
        self.assertEqual(report["status"], "clean")
        self.assertEqual(report["initial_decisions"][0]["verdict"], "reject")
        self.assertEqual(len(self.calls()), 4)
        decision = report["initial_decisions"][0]
        self.assertEqual(decision["adversarial_challenge"]["assessment"], "pre_existing")
        self.assertEqual(decision["reviewer_response"]["assessment"], "pre_existing")

    def test_reviewer_can_defend_a_finding_the_adversary_wants_to_drop(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_ASSESSMENT": "pre_existing",
                "FAKE_REVIEWER_ASSESSMENT": "confirmed",
            },
        ):
            report = ReviewWorkflow(self.config()).run()
        decision = report["initial_decisions"][0]
        self.assertEqual(report["status"], "action_required")
        self.assertEqual(decision["adversarial_challenge"]["assessment"], "pre_existing")
        self.assertEqual(decision["reviewer_response"]["assessment"], "confirmed")
        self.assertEqual(decision["verdict"], "accept")
        self.assertEqual(len(self.calls()), 4)

    def test_confirmed_findings_do_not_trigger_a_manufactured_rebuttal(self) -> None:
        report = ReviewWorkflow(self.config()).run()
        decision = report["initial_decisions"][0]
        self.assertEqual(decision["adversarial_challenge"]["assessment"], "confirmed")
        self.assertIsNone(decision["reviewer_response"])
        self.assertEqual(len(self.calls()), 3)

    def test_low_impact_downgrade_gets_a_reviewer_rebuttal(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_IMPACT": "low"}):
            report = ReviewWorkflow(self.config()).run()
        decision = report["initial_decisions"][0]
        self.assertEqual(decision["verdict"], "non_blocking")
        self.assertEqual(decision["reviewer_response"]["assessment"], "confirmed")
        self.assertEqual(len(self.calls()), 4)

    def test_fix_runs_one_batch_and_one_final_review(self) -> None:
        report = ReviewWorkflow(self.config(mode="fix")).run()
        self.assertEqual(report["status"], "clean")
        self.assertIn("max(value - 1, 0)", (self.fixture.repo / "app.py").read_text())
        self.assertEqual(report["repair_churn"]["production_added"], 1)
        self.assertEqual(report["final_findings"], [])
        self.assertEqual([call["schema"] for call in self.calls()],
                         [None, "observation.json", "challenge.json", "fix.json", None])

    def test_each_role_has_appropriate_sandbox_and_no_session_reuse(self) -> None:
        report = ReviewWorkflow(self.config(mode="fix")).run()
        for call in self.calls():
            argv = call["argv"]
            self.assertIn("--ephemeral", argv)
            sandbox = argv[argv.index("--sandbox") + 1]
            self.assertEqual(
                sandbox,
                "workspace-write" if call["schema"] == "fix.json" else "read-only",
            )
            if call["schema"]:
                self.assertIn("skills.include_instructions=false", argv)
                for capability in ("multi_agent", "apps", "plugins", "memories"):
                    self.assertIn(capability, argv)
        self.assertEqual(report["usage"]["turn_count"], 5)

    def test_blind_prompt_does_not_reveal_original_finding(self) -> None:
        report = ReviewWorkflow(self.config()).run()
        finding_id = report["initial_findings"][0]["finding_id"]
        prompt = Path(
            report["artifacts_dir"], f"initial-blind-{finding_id}.prompt.txt"
        ).read_text()
        self.assertIn(finding_id, prompt)
        self.assertNotIn("Clamp negative values", prompt)
        self.assertNotIn("[P1]", prompt)

    def test_all_priorities_receive_full_adversarial_review(self) -> None:
        for priority in ("2", "3"):
            with self.subTest(priority=priority):
                self.state.write_text("[]", encoding="utf-8")
                with mock.patch.dict(os.environ, {"FAKE_PRIORITY": priority}):
                    report = ReviewWorkflow(self.config()).run()
                self.assertEqual(report["status"], "action_required")
                self.assertEqual(report["initial_findings"][0]["priority"], int(priority))
                self.assertEqual(report["initial_decisions"][0]["verdict"], "accept")
                self.assertEqual(
                    [call["schema"] for call in self.calls()],
                    [None, "observation.json", "challenge.json"],
                )

    def test_unproven_low_priority_finding_requires_human_review(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_PRIORITY": "3",
                "FAKE_ASSESSMENT": "pre_existing",
                "FAKE_EVIDENCE": "hypothetical",
            },
        ):
            report = ReviewWorkflow(self.config()).run()
        self.assertEqual(report["status"], "human_review_required")
        self.assertEqual(report["initial_decisions"][0]["verdict"], "human_review")

    def test_invalid_structured_output_is_sent_to_human_review(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_INVALID_SCHEMA": "1"}):
            report = ReviewWorkflow(self.config()).run()
        self.assertEqual(report["status"], "human_review_required")
        self.assertEqual(report["initial_decisions"][0]["verdict"], "human_review")

    def test_ungrounded_severe_refutation_is_sent_to_human_review(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"FAKE_ASSESSMENT": "pre_existing", "FAKE_EVIDENCE": "hypothetical"},
        ):
            report = ReviewWorkflow(self.config(mode="fix")).run()
        self.assertEqual(report["status"], "human_review_required")
        self.assertIsNone(report["repair"])

    def test_explicit_check_runs_once_after_repair(self) -> None:
        report = ReviewWorkflow(
            self.config(mode="fix", checks=("git status --porcelain",))
        ).run()
        self.assertEqual(report["status"], "clean")
        self.assertEqual(len(report["repair"]["verified_checks"]), 1)
        self.assertTrue(Path(report["repair"]["verified_checks"][0]["output"]).is_file())

    def test_existing_review_file_skips_initial_native_call(self) -> None:
        review_path = self.fixture.root / "existing-review.txt"
        review_path.write_text(
            f"- [P1] Clamp negative values — {self.fixture.repo / 'app.py'}:2-2\n"
            "  Negative values escape validation.\n",
            encoding="utf-8",
        )
        report = ReviewWorkflow(self.config(review_file=review_path)).run()
        self.assertEqual(report["status"], "action_required")
        self.assertEqual([call["schema"] for call in self.calls()],
                         ["observation.json", "challenge.json"])

    def test_fix_refuses_initially_dirty_worktree(self) -> None:
        self.fixture.write("app.py", "def validate(value):\n    return 10\n")
        with self.assertRaisesRegex(ReviewReducerError, "clean working tree"):
            ReviewWorkflow(self.config(mode="fix")).run()
        self.assertFalse(self.state.exists())

    def test_fix_rejects_generated_untracked_files(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_REPAIR": "untracked"}):
            with self.assertRaisesRegex(BudgetExceededError, "untracked files"):
                ReviewWorkflow(self.config(mode="fix")).run()
        self.assertTrue((self.fixture.repo / "untracked.py").is_file())

    def test_fix_rejects_measured_excessive_churn(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_REPAIR": "budget"}):
            with self.assertRaisesRegex(BudgetExceededError, "production lines"):
                ReviewWorkflow(
                    self.config(mode="fix", policy=ReviewPolicy(max_added_production_lines=5))
                ).run()
        self.assertIn("unnecessary line", (self.fixture.repo / "app.py").read_text())

    def test_blind_verifier_can_be_disabled(self) -> None:
        report = ReviewWorkflow(self.config(blind_verification=False)).run()
        self.assertEqual(report["status"], "action_required")
        self.assertEqual([call["schema"] for call in self.calls()], [None, "challenge.json"])

    def test_cli_exit_codes_reflect_verified_findings(self) -> None:
        code = main([
            "review", "--repo", str(self.fixture.repo), "--base", "main",
            "--codex-bin", str(self.binary), "--jobs", "1",
        ])
        self.assertEqual(code, 2)

    def test_rejects_artifacts_inside_reviewed_worktree(self) -> None:
        with self.assertRaisesRegex(ReviewReducerError, "artifact directory"):
            ReviewWorkflow(
                self.config(artifacts_dir=self.fixture.repo / "reports")
            ).run()
        self.assertFalse(self.state.exists())

    def test_dashboard_keeps_json_stdout_machine_readable(self) -> None:
        stderr = io.StringIO()
        stdout = io.StringIO()
        with mock.patch("sys.stderr", stderr), redirect_stdout(stdout):
            code = main([
                "review", "--repo", str(self.fixture.repo), "--base", "main",
                "--codex-bin", str(self.binary), "--jobs", "1",
                "--json", "--progress", "always",
            ])
        self.assertEqual(code, 2)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "action_required")
        self.assertIn("C O D E X   R E V I E W", stderr.getvalue())
        self.assertIn("\x1b[?25h", stderr.getvalue())

    def test_codex_events_stream_into_dashboard_state(self) -> None:
        workflow = ReviewWorkflow(self.config())
        report = workflow.run()
        self.assertEqual(report["status"], "action_required")
        self.assertEqual(workflow.display.state.tool_calls, 3)
        self.assertEqual(workflow.display.state.turns, 3)
        self.assertTrue(workflow.display.state.agents)
        self.assertTrue(all(agent.finished for agent in workflow.display.state.agents.values()))

    def test_dashboard_tracks_the_complete_repair_pipeline(self) -> None:
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            report = ReviewWorkflow(self.config(mode="fix", progress="always")).run()
        self.assertEqual(report["status"], "clean")
        dashboard = stderr.getvalue()
        self.assertIn("✓ review", dashboard)
        self.assertIn("✓ challenge", dashboard)
        self.assertIn("✓ repair", dashboard)
        self.assertIn("✓ final", dashboard)
        self.assertIn("CLEAN", dashboard)
        self.assertIn("5 source checks", dashboard)

    def test_session_persists_all_finding_evidence(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_ASSESSMENT": "pre_existing"}):
            report = ReviewWorkflow(self.config()).run()
        session = ReviewSession.open(Path(report["artifacts_dir"]))
        self.assertEqual(session.data["state"], "complete")
        self.assertEqual(session.data["session_id"], report["session_id"])
        investigation = session.data["findings"][0]["investigations"]["initial"]
        self.assertEqual(investigation["observation"]["changed_behavior"], "Validation subtracts one.")
        self.assertEqual(investigation["adversary"]["assessment"], "pre_existing")
        self.assertEqual(investigation["reviewer_response"]["assessment"], "pre_existing")
        self.assertEqual(session.data["findings"][0]["decision"]["verdict"], "reject")

    def test_saved_session_manual_dismiss_preserves_model_decision(self) -> None:
        report = ReviewWorkflow(self.config()).run()
        session = ReviewSession.open(Path(report["artifacts_dir"]))
        entry = session.override("1", "dismiss", "Already safe on the parent branch")
        self.assertEqual(entry["decision"]["verdict"], "accept")
        self.assertEqual(entry["manual_override"]["action"], "dismiss")
        self.assertEqual(session.data["status"], "clean")
        self.assertEqual(session.effective_decisions()[0].verdict.value, "reject")
        self.assertEqual(entry["history"][-1]["action"], "dismiss")

    def test_saved_session_manual_include_and_reset(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_ASSESSMENT": "pre_existing"}):
            report = ReviewWorkflow(self.config()).run()
        session = ReviewSession.open(Path(report["artifacts_dir"]))
        finding_id = session.data["findings"][0]["finding"]["finding_id"]
        session.override(finding_id[:8], "include", "The changed caller exposes it")
        self.assertEqual(session.data["status"], "action_required")
        self.assertEqual(session.effective_decisions()[0].verdict.value, "accept")
        self.assertTrue(session.effective_decisions()[0].auto_fix_allowed)
        session.override("1", "reset")
        self.assertEqual(session.data["status"], "clean")
        self.assertEqual(session.data["findings"][0]["decision"]["verdict"], "reject")

    def test_saved_session_list_and_detail_commands(self) -> None:
        report = ReviewWorkflow(self.config()).run()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["session", "list", "--repo", str(self.fixture.repo), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())[0]["session_id"], report["session_id"])

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main([
                "session", "show", "latest", "--finding", "1",
                "--repo", str(self.fixture.repo), "--json",
            ])
        self.assertEqual(code, 0)
        entry = json.loads(stdout.getvalue())
        self.assertEqual(entry["decision"]["verdict"], "accept")
        self.assertIn("adversary", entry["investigations"]["initial"])

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main([
                "session", "dismiss", "latest", "1", "--repo", str(self.fixture.repo),
                "--reason", "Inherited parent behavior", "--json",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["manual_override"]["action"], "dismiss")
        self.assertEqual(list_sessions(self.fixture.repo)[0].data["status"], "clean")
        self.assertIn("DISMISSED", Path(report["html_report"]).read_text())

    def test_saved_session_report_regenerates_without_codex_turns(self) -> None:
        report = ReviewWorkflow(self.config()).run()
        original_calls = self.calls()
        output = self.fixture.root / "shareable-review.html"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main([
                "session", "report", "latest", "--repo", str(self.fixture.repo),
                "--output", str(output), "--no-open-report", "--json",
            ])
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["html_report"], str(output.resolve()))
        self.assertEqual(self.calls(), original_calls)
        self.assertIn("Blind investigation", output.read_text())
        self.assertIn(report["session_id"], output.read_text())

    def ask(self, question: str, *options: str) -> tuple[int, str]:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main([
                "session", "ask", "latest", "1", question,
                "--repo", str(self.fixture.repo),
                "--codex-bin", str(self.binary),
                "--no-open-report", *options,
            ])
        return code, stdout.getvalue()

    def test_followup_answer_is_persisted_without_changing_the_verdict(self) -> None:
        report = ReviewWorkflow(self.config()).run()
        code, output = self.ask("Can this be fixed in one line?")
        self.assertEqual(code, 0)
        self.assertIn("one-line clamp", output)
        self.assertIn("saved verdict is unchanged", output)
        saved = ReviewSession.open(Path(report["artifacts_dir"]))
        entry = saved.data["findings"][0]
        self.assertEqual(entry["decision"]["verdict"], "accept")
        self.assertEqual(len(entry["questions"]), 1)
        self.assertEqual(entry["questions"][0]["question"], "Can this be fixed in one line?")
        self.assertEqual(entry["questions"][0]["perspective"], "neutral")
        self.assertEqual(entry["history"][-1]["action"], "question")
        self.assertEqual([call["schema"] for call in self.calls()], [
            None, "observation.json", "challenge.json", "followup.json"
        ])
        document = Path(report["html_report"]).read_text()
        self.assertIn("Follow-up question · 1", document)
        self.assertIn("Can this be fixed in one line?", document)
        self.assertIn("one-line clamp", document)

    def test_followup_can_explain_a_resolved_finding_after_guarded_repair(self) -> None:
        report = ReviewWorkflow(self.config(mode="fix")).run()
        original = ReviewSession.open(Path(report["artifacts_dir"]))
        self.assertTrue(original.data["findings"][0]["resolved"])

        code, output = self.ask("Why does the repaired return resolve this finding?")

        self.assertEqual(code, 0)
        self.assertIn("one-line clamp", output)
        entry = ReviewSession.open(Path(report["artifacts_dir"])).data["findings"][0]
        self.assertTrue(entry["resolved"])
        self.assertEqual(len(entry["questions"]), 1)
        self.assertEqual(self.calls()[-1]["schema"], "followup.json")
        finding_id = entry["finding"]["finding_id"]
        prompt = Path(
            report["artifacts_dir"], f"followup-{finding_id}-001.prompt.txt"
        ).read_text()
        self.assertIn('"resolved": true', prompt)

    def test_followup_perspectives_and_prior_questions_are_preserved(self) -> None:
        report = ReviewWorkflow(self.config()).run()
        first, _ = self.ask("Could this be inherited?", "--perspective", "adversary")
        second, _ = self.ask("Why should we keep it?", "--perspective", "reviewer")
        self.assertEqual((first, second), (0, 0))
        saved = ReviewSession.open(Path(report["artifacts_dir"]))
        questions = saved.data["findings"][0]["questions"]
        self.assertEqual([item["perspective"] for item in questions], ["adversary", "reviewer"])
        self.assertEqual([item["question_id"] for item in questions], [1, 2])
        finding_id = saved.data["findings"][0]["finding"]["finding_id"]
        first_prompt = Path(
            report["artifacts_dir"], f"followup-{finding_id}-001.prompt.txt"
        ).read_text()
        second_prompt = Path(
            report["artifacts_dir"], f"followup-{finding_id}-002.prompt.txt"
        ).read_text()
        self.assertIn("Perspective: adversary", first_prompt)
        self.assertIn("Perspective: reviewer", second_prompt)
        self.assertIn("Could this be inherited?", second_prompt)
        self.assertIn("Follow-up questions · 2", Path(report["html_report"]).read_text())

    def test_followup_json_is_machine_readable_and_updates_usage(self) -> None:
        report = ReviewWorkflow(self.config()).run()
        code, output = self.ask("Where is the changed return?", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["question"], "Where is the changed return?")
        self.assertEqual(payload["response"]["source_anchors"][0]["path"], "app.py")
        saved = ReviewSession.open(Path(report["artifacts_dir"]))
        self.assertEqual(saved.data["usage"]["turn_count"], 4)
        persisted_report = json.loads(
            Path(report["artifacts_dir"], "report.json").read_text()
        )
        self.assertEqual(persisted_report["usage"]["turn_count"], 4)

    def test_followup_uses_a_fresh_read_only_turn_and_requested_model(self) -> None:
        ReviewWorkflow(self.config()).run()
        code, _ = self.ask(
            "Which source establishes the caller contract?",
            "--model", "focused-review-model",
            "--reasoning-effort", "high",
            "--progress", "never",
        )

        self.assertEqual(code, 0)
        argv = self.calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(argv[argv.index("--model") + 1], "focused-review-model")
        self.assertIn("model_reasoning_effort=\"high\"", argv)
        self.assertIn("--ephemeral", argv)

    def test_followup_does_not_reclassify_finding_without_manual_override(self) -> None:
        report = ReviewWorkflow(self.config()).run()
        with mock.patch.dict(os.environ, {"FAKE_FOLLOWUP_VERDICT": "reject"}):
            code, output = self.ask("Should this be dismissed?", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["response"]["suggested_verdict"], "reject")
        saved = ReviewSession.open(Path(report["artifacts_dir"]))
        self.assertEqual(saved.data["findings"][0]["decision"]["verdict"], "accept")
        self.assertIsNone(saved.data["findings"][0]["manual_override"])

    def test_followup_refuses_a_changed_head_before_invoking_codex(self) -> None:
        ReviewWorkflow(self.config()).run()
        original_calls = self.calls()
        self.fixture.write("app.py", "def validate(value):\n    return value - 2\n")
        self.fixture.commit("change reviewed head")
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code, _ = self.ask("Does the original issue still happen?")
        self.assertEqual(code, 1)
        self.assertIn("no longer matches", stderr.getvalue())
        self.assertEqual(self.calls(), original_calls)

    def test_followup_refuses_a_changed_working_tree(self) -> None:
        ReviewWorkflow(self.config()).run()
        original_calls = self.calls()
        self.fixture.write("app.py", "def validate(value):\n    return value + 8\n")
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code, _ = self.ask("Does this still reach callers?")
        self.assertEqual(code, 1)
        self.assertIn("reviewed tracked patch", stderr.getvalue())
        self.assertEqual(self.calls(), original_calls)

    def test_followup_rejects_unsafe_or_invalid_source_anchors(self) -> None:
        for anchor in ("outside", "missing", "bad_line", "none"):
            with self.subTest(anchor=anchor):
                fixture = GitFixture()
                try:
                    binary = fixture.root / "fake-codex"
                    binary.write_text(_FAKE_CODEX, encoding="utf-8")
                    binary.chmod(0o755)
                    state = fixture.root / "state.json"
                    with mock.patch.dict(
                        os.environ,
                        {"FAKE_CODEX_STATE": str(state), "FAKE_FOLLOWUP_ANCHOR": anchor},
                    ):
                        report = ReviewWorkflow(
                            RunConfig(repo=fixture.repo, base="main", codex_bin=str(binary), jobs=1)
                        ).run()
                        stderr = io.StringIO()
                        stdout = io.StringIO()
                        with mock.patch("sys.stderr", stderr), redirect_stdout(stdout):
                            code = main([
                                "session", "ask", "latest", "1", "Check the source",
                                "--repo", str(fixture.repo), "--codex-bin", str(binary),
                                "--no-open-report",
                            ])
                        self.assertEqual(code, 1)
                        saved = ReviewSession.open(Path(report["artifacts_dir"]))
                        self.assertEqual(saved.data["findings"][0].get("questions", []), [])
                finally:
                    fixture.cleanup()

    def test_followup_rejects_malformed_output_and_mismatched_identifiers(self) -> None:
        for variable in ("FAKE_FOLLOWUP_INVALID_SCHEMA", "FAKE_FOLLOWUP_WRONG_ID"):
            with self.subTest(variable=variable):
                report = ReviewWorkflow(self.config()).run()
                with mock.patch.dict(os.environ, {variable: "1"}):
                    stderr = io.StringIO()
                    with mock.patch("sys.stderr", stderr):
                        code, _ = self.ask("Can we prove this?")
                self.assertEqual(code, 1)
                saved = ReviewSession.open(Path(report["artifacts_dir"]))
                self.assertEqual(saved.data["findings"][0].get("questions", []), [])
                self.state.write_text("[]", encoding="utf-8")

    def test_followup_rejects_invalid_confidence_without_saving_an_answer(self) -> None:
        report = ReviewWorkflow(self.config()).run()
        with mock.patch.dict(os.environ, {"FAKE_FOLLOWUP_CONFIDENCE": "1.2"}):
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                code, _ = self.ask("How certain is the source evidence?")

        self.assertEqual(code, 1)
        self.assertIn("confidence must be between zero and one", stderr.getvalue())
        saved = ReviewSession.open(Path(report["artifacts_dir"]))
        self.assertEqual(saved.data["findings"][0].get("questions", []), [])

    def test_followup_detects_source_drift_during_the_model_turn(self) -> None:
        report = ReviewWorkflow(self.config()).run()
        with mock.patch.dict(os.environ, {"FAKE_FOLLOWUP_DRIFT": "1"}):
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                code, _ = self.ask("Can this be simplified?")
        self.assertEqual(code, 1)
        self.assertIn("tracked working-tree patch changed", stderr.getvalue())
        saved = ReviewSession.open(Path(report["artifacts_dir"]))
        self.assertEqual(saved.data["findings"][0].get("questions", []), [])

    def test_html_report_cannot_dirty_reviewed_working_tree(self) -> None:
        ReviewWorkflow(self.config()).run()
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code = main([
                "session", "report", "latest", "--repo", str(self.fixture.repo),
                "--output", str(self.fixture.repo / "report.html"), "--no-open-report",
            ])
        self.assertEqual(code, 1)
        self.assertIn("cannot live inside the reviewed working tree", stderr.getvalue())
        self.assertFalse((self.fixture.repo / "report.html").exists())

    def test_review_no_open_report_preserves_json_stdout(self) -> None:
        stdout = io.StringIO()
        with (
            redirect_stdout(stdout),
            mock.patch("review_reducer.cli.webbrowser.open") as browser,
        ):
            code = main([
                "review", "--repo", str(self.fixture.repo), "--base", "main",
                "--codex-bin", str(self.binary), "--jobs", "1", "--json",
                "--no-open-report",
            ])
        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(Path(payload["html_report"]).is_file())
        browser.assert_not_called()

    def test_saved_session_apply_repairs_curated_finding_once(self) -> None:
        report = ReviewWorkflow(self.config()).run()
        saved = ReviewSession.open(Path(report["artifacts_dir"]))
        final = ReviewWorkflow(self.config(mode="fix")).apply_session(saved)
        self.assertEqual(final["status"], "clean")
        self.assertEqual(final["session_id"], report["session_id"])
        self.assertIn("max(value - 1, 0)", (self.fixture.repo / "app.py").read_text())
        self.assertEqual(
            [call["schema"] for call in self.calls()],
            [None, "observation.json", "challenge.json", "fix.json", None],
        )
        persisted = ReviewSession.open(saved.run_dir)
        self.assertEqual(persisted.data["status"], "clean")
        self.assertEqual(persisted.data["summary"]["resolved"], 1)

    def test_repair_marks_only_applied_findings_resolved(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_SECOND_FINDING": "1"}):
            report = ReviewWorkflow(self.config(mode="fix")).run()
        session = ReviewSession.open(Path(report["artifacts_dir"]))
        self.assertEqual(session.data["summary"]["resolved"], 1)
        self.assertEqual(session.data["summary"]["rejected"], 1)
        self.assertEqual(session.data["status"], "clean")

    def test_saved_session_apply_command_uses_curated_session(self) -> None:
        report = ReviewWorkflow(self.config()).run()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main([
                "session", "apply", "latest", "--repo", str(self.fixture.repo),
                "--codex-bin", str(self.binary), "--jobs", "1", "--json",
            ])
        self.assertEqual(code, 0)
        final = json.loads(stdout.getvalue())
        self.assertEqual(final["session_id"], report["session_id"])
        self.assertEqual(final["status"], "clean")

    def test_manually_included_refuted_finding_can_use_its_bounded_fix(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_ASSESSMENT": "pre_existing"}):
            report = ReviewWorkflow(self.config()).run()
        saved = ReviewSession.open(Path(report["artifacts_dir"]))
        saved.override("1", "include", "Changed caller now exposes the inherited defect")
        final = ReviewWorkflow(self.config(mode="fix")).apply_session(saved)
        self.assertEqual(final["status"], "clean")
        self.assertEqual(ReviewSession.open(saved.run_dir).data["summary"]["resolved"], 1)

    def test_legacy_session_is_inspectable_but_unverified_include_cannot_apply(self) -> None:
        report = ReviewWorkflow(self.config()).run()
        run_dir = Path(report["artifacts_dir"])
        decision = report["initial_decisions"][0]
        report["initial_decisions"] = [{
            "finding": decision["finding"],
            "verdict": "non_blocking",
            "reason": "finding is below the configured blocking priority",
            "challenge": None,
            "observation": None,
            "blocks_review": False,
            "auto_fix_allowed": False,
        }]
        (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (run_dir / "session.json").unlink()
        saved = ReviewSession.open(run_dir)
        self.assertEqual(saved.data["findings"][0]["decision"]["verdict"], "non_blocking")
        saved.override("1", "include", "This lower-priority issue matters")
        with self.assertRaisesRegex(ReviewReducerError, "does not have a verified"):
            ReviewWorkflow(self.config(mode="fix")).apply_session(saved)
        self.assertEqual(ReviewSession.open(run_dir).data["state"], "complete")

    def test_saved_session_apply_rejects_changed_head(self) -> None:
        report = ReviewWorkflow(self.config()).run()
        saved = ReviewSession.open(Path(report["artifacts_dir"]))
        self.fixture.write("app.py", "def validate(value):\n    return value - 2\n")
        self.fixture.commit("change reviewed head")
        with self.assertRaisesRegex(ReviewReducerError, "no longer matches"):
            ReviewWorkflow(self.config(mode="fix")).apply_session(saved)

    def test_failed_run_retains_inspectable_session(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_REPAIR": "untracked"}):
            with self.assertRaises(BudgetExceededError):
                ReviewWorkflow(self.config(mode="fix")).run()
        saved = list_sessions(self.fixture.repo)[0]
        self.assertEqual(saved.data["state"], "failed")
        self.assertIn("untracked files", saved.data["failure"])
        self.assertEqual(saved.data["findings"][0]["decision"]["verdict"], "accept")


if __name__ == "__main__":
    unittest.main()
