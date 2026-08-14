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

Findings are advisory by default. The command exits successfully when no
source-verified blocking issues survive, exits `2` when verified issues remain,
and exits `3` when a severe claim requires human judgment.

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
3. For each potentially blocking finding, run a separate blind, read-only Codex
   session that sees the source location but not the alleged defect.
4. Run a fresh adversarial Codex session that receives the finding and blind
   observations. Require exact-base comparison, realistic reachability, concrete
   source anchors, independently evidenced user impact, preservation of the
   pull request's intent, and the smallest direct fix.
5. Deterministically reject source-refuted inherited, intentional, unreachable,
   speculative, or duplicate findings. Never silently discard an unproven P0/P1;
   route it to human review instead.
6. In `fix` mode only, send surviving findings to one workspace-write Codex
   session. Reject new files, dependency changes, public APIs, excess production
   lines, and excessive production-file churn.
7. Run only explicitly requested checks, followed by one final native review and
   the same verification policy. Stop there even if new findings remain.

Native review is intentionally kept separate from structured verification:
`codex exec review --output-schema` does not enforce the requested schema for
the built-in reviewer. The verifier, adjudicator, and fixer therefore use fresh
ordinary `codex exec --output-schema` sessions instead.

## Useful options

```text
--mode review|fix                 Report only, or apply one verified fix batch.
--max-priority 1                  Treat P0/P1 as blocking; preserve P2/P3 in reports.
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
--check COMMAND                   Run only this explicitly authorized repair check.
--json                            Emit the complete machine-readable report.
```

Run artifacts are private to the local user and default to
`<git-common-dir>/review-reducer/<timestamp>-<head>/`. They include the pinned
snapshot, role prompts, JSONL events, final model responses, structured
decisions, measured repair churn, explicit check output, and the final report.
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
