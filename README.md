# Codex Review Reducer

A small, dependency-free proof of concept that reduces noisy Codex pull-request
findings before they reach a human reviewer. It can also apply one conservative
batch of verified fixes and perform one final review.

The associated [research](RESEARCH.md) explains the evidence, alternatives, and
design constraints in more detail.

## Quick start

Run the checkout directly:

```sh
./review-reducer review --repo ~/code/my-project --base origin/main
```

For a stacked pull request, set `--base` to its immediate parent branch instead
of `origin/main`; otherwise inherited parent changes can produce false findings.

Every P0–P3 finding gets the same evidence-grounded review. Priority is retained
as useful context, but never excludes an issue from investigation. The command
exits successfully when no consequential verified issues survive, exits `2` when
verified issues remain, and exits `3` when a claim requires human judgment.

Interactive terminals show a live dashboard with the current review stage,
parallel Codex agents, recent source-inspection activity, finding decisions,
elapsed time, and token usage. Redirected output automatically falls back to
plain progress lines. Force or disable the dashboard when necessary:

```text
╭──────────────────────────────────────────────────────────────────────────╮
│ ◈  C O D E X   R E V I E W   R E D U C E R             ⠹ LIVE  00:18     │
│ PIPELINE                                                                │
│ ✓ review   ⠹ challenge   ○ repair   ○ final                             │
│                                                                         │
│ ACTIVE AGENTS                                                           │
│ ⠹ adversarial review · src/app.py:42                                    │
│   Comparing the changed caller contract against the exact merge base.   │
│                                                                         │
│ FINDINGS  3 found   1 accepted   1 rejected   0 human                   │
│ ✓ P1 Existing guard already rejects invalid input  [REJECTED]           │
│ ⠹ P1 Preserve caller contract  [CHALLENGING]                           │
╰──────────────────────────────────────────────────────────────────────────╯
```

```sh
./review-reducer review --repo ~/code/my-project --progress always
./review-reducer review --repo ~/code/my-project --progress never
```

The dashboard writes only to stderr, so `--json` remains valid JSON on stdout.
Set `NO_COLOR=1` to preserve the dashboard without ANSI colors.

## Inspect and curate a saved session

Every run immediately creates a durable local session. List the sessions for a
repository, inspect the latest results, or drill into one finding by its
1-based number or identifier prefix:

```sh
./review-reducer session list --repo ~/code/my-project
./review-reducer session show latest --repo ~/code/my-project
./review-reducer session show latest --finding 1 --repo ~/code/my-project
```

A finding's detailed view includes the original native-review comment, the
independent blind investigation, the adversary's assessment, the original
reviewer's rebuttal when one was warranted, and the final model decision. Add
`--json` to any session command for the complete machine-readable evidence.

After inspecting the evidence, explicitly include, dismiss, or reset a finding
without overwriting the model's original decision:

```sh
./review-reducer session dismiss latest 1 \
  --repo ~/code/my-project \
  --reason 'Behavior already exists on the exact merge base'

./review-reducer session include latest 0504f58a \
  --repo ~/code/my-project \
  --reason 'Confirmed user-visible failure is worth the direct fix'

./review-reducer session reset latest 1 --repo ~/code/my-project
```

Apply the curated findings later as one bounded repair batch followed by one
final native review:

```sh
./review-reducer session apply latest --repo ~/code/my-project
```

The saved repository, HEAD, base, merge base, and complete patch must still
match. Manually included findings remain ineligible for automatic repair unless
the saved evidence identifies an intent-preserving direct fix that stays inside
the same complexity and dependency budgets.

Older artifact directories remain inspectable even when they predate
`session.json`. To fully investigate findings that an older run skipped, reuse
its saved native review instead of spending another native-review turn:

```sh
./review-reducer review \
  --repo ~/code/my-project \
  --base origin/main \
  --review-file /path/to/previous-run/initial.response.txt
```

To allow one small automatic repair and exactly one final native review:

```sh
./review-reducer review \
  --repo ~/code/my-project \
  --base origin/main \
  --mode fix \
  --max-added-production-lines 12 \
  --max-additional-production-files 2
```

The target must start with a clean working tree. The tool never commits, pushes,
publishes a review, resolves threads, creates new working-tree files, or
repeatedly repairs the same branch. Generated changes remain unstaged for
inspection.

Optionally request an explicit check after the repair:

```sh
./review-reducer review \
  --repo ~/code/my-project \
  --base origin/main \
  --mode fix \
  --check 'cargo test -p my-crate focused_test_name'
```

The reducer itself invokes a repository check only when `--check` explicitly
asks for it. Verification prompts prohibit running repository code, hooks,
builds, or tests. Codex's native built-in reviewer has its own fixed prompt and
may use tools permitted by its read-only sandbox. Explicit check commands
execute without shell interpretation.

The package can also be installed locally with `python3 -m pip install -e .`.
When running without installation in an environment that sets
`PYTHONSAFEPATH=1`, use the executable above or:

```sh
PYTHONPATH=. python3 -m review_reducer review --repo ~/code/my-project
```

## How it works

1. Pin the target HEAD, exact merge base, complete tracked diff, and original
   production/test churn.
2. Run Codex's built-in native reviewer in a fresh, ephemeral, read-only session.
3. For every native finding, regardless of priority, run a separate blind,
   read-only Codex session that sees the source location but not the alleged
   defect.
4. Run a fresh adversarial Codex session that receives the finding and blind
   observations. Require exact-base comparison, realistic reachability, concrete
   source anchors, independently evidenced user impact, preservation of the
   pull request's intent, and the smallest direct fix.
5. When the adversary believes dismissal or downgrading is genuinely useful,
   give the original-review perspective one evidence-grounded chance to rebut
   or concede. Confirmed useful findings do not incur a manufactured debate.
6. Deterministically reject source-refuted inherited, intentional, unreachable,
   speculative, or duplicate findings. Route unproven claims of any priority to
   human review instead of silently discarding them.
7. In `fix` mode only, send surviving findings to one workspace-write Codex
   session. Reject new files, dependency changes, public APIs, excess production
   lines, and excessive production-file churn.
8. Run only explicitly requested checks, followed by one final native review and
   the same verification policy. Stop there even if new findings remain.

Native review is intentionally kept separate from structured verification:
`codex exec review --output-schema` does not enforce the requested schema for
the built-in reviewer. The verifier, adjudicator, and fixer therefore use fresh
ordinary `codex exec --output-schema` sessions instead.

## Useful options

```text
--mode review|fix                 Report only, or apply one verified fix batch.
--min-confidence 0.75             Minimum adversarial confidence for automatic fixes.
--max-added-production-lines 20   Hard limit checked against the actual repair.
--max-additional-production-files 2
--max-findings 12                 Fail safely on unexpectedly large review outputs.
--jobs 2                          Number of independent verification workers.
--review-model MODEL              Override Codex's native review model.
--verifier-model MODEL            Override the blind/adversarial model.
--fixer-model MODEL               Override the repair model.
--review-file PATH                Reuse an existing rendered or JSON-shaped review.
--artifacts-dir PATH              Store artifacts outside the reviewed working tree.
--no-blind-verification           Skip the independent blind source investigation.
--progress auto|always|never      Control the live terminal dashboard.
--check COMMAND                   Run only this explicitly authorized repair check.
--json                            Emit the complete machine-readable report.
```

Run artifacts are private to the local user and default to
`<git-common-dir>/review-reducer/<timestamp>-<head>/`. They include the pinned
snapshot, role prompts, JSONL events, final model responses, structured
decisions, measured repair churn, explicit check output, the final report, and a
continuously updated `session.json` with per-finding evidence and manual
decision history. Existing artifact directories from older runs can still be
inspected by passing their path as the session selector; findings that were
never investigated must be reviewed again before they can be automatically
repaired.
The report also records actual per-role input, cached-input, output, and
reasoning-token usage. Structured roles disable apps, plugins, memories,
multi-agent spawning, and automatic skill-instruction injection to reduce
context size and prevent unrelated side effects.

## Safety and limitations

- Repository content and prior model findings are treated as untrusted evidence.
- Review and verification are read-only; only the single repair role may edit.
- A dirty or untracked target is rejected before automatic repair begins.
- Untracked files created by a repair are rejected because native base review
  does not include them.
- Hard repair-budget failures preserve the generated working-tree changes for
  inspection; they do not attempt an unsafe automatic rollback.
- Static source inspection is not presented as runtime-observed evidence.
- Model-role isolation reduces anchoring but does not make same-model judgments
  statistically independent.
- Codex's built-in reviewer still processes applicable project instructions;
  hostile repository instruction files require external trust controls.

## Development

```sh
PYTHONPATH=. python3 -m unittest discover -s tests -v
./review-reducer --help
```
