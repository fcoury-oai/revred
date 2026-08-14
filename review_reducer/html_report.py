"""Beautiful, offline-safe HTML reports rendered from saved review evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
import json
from pathlib import Path
import re
import shlex
from typing import Any

from review_reducer.errors import ReviewReducerError
from review_reducer.git import git_common_dir
from review_reducer.sessions import ReviewSession


_PRIORITY = re.compile(r"^\s*\[P[0-3]\]\s*")
_SPACE = re.compile(r"\s+")
_SAFE_ANCHOR = re.compile(r"[^a-zA-Z0-9_-]+")

_CSS = """
:root {
  color-scheme: dark;
  --bg: #080a11;
  --surface: rgba(18, 21, 33, .84);
  --surface-strong: #151927;
  --border: rgba(203, 213, 255, .12);
  --muted: #99a3bb;
  --ink: #f2f4fc;
  --violet: #aa94ff;
  --cyan: #73d8e9;
  --green: #77dfae;
  --amber: #ffcb80;
  --rose: #ff8797;
  --radius: 22px;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  min-height: 100vh;
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--ink);
  background:
    radial-gradient(ellipse at 12% 0, rgba(106, 85, 188, .20), transparent 36%),
    radial-gradient(ellipse at 90% 13%, rgba(65, 162, 183, .14), transparent 34%),
    var(--bg);
}
a { color: var(--cyan); text-decoration: none; }
a:hover { text-decoration: underline; }
.shell { max-width: 1220px; margin: 0 auto; padding: 66px 32px 88px; }
.eyebrow, .label {
  color: var(--muted);
  font-size: 11px;
  letter-spacing: .12em;
  text-transform: uppercase;
}
.masthead { display: flex; justify-content: space-between; align-items: center; gap: 20px; }
.brand { display: flex; align-items: center; gap: 11px; font-size: 14px; font-weight: 650; }
.brand-mark {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  color: var(--cyan);
  border: 1px solid rgba(115, 216, 233, .25);
  border-radius: 11px;
  background: rgba(115, 216, 233, .08);
}
.masthead-meta { color: var(--muted); font-size: 12px; text-align: right; }
.hero { padding: 72px 0 38px; }
.hero h1 {
  max-width: 850px;
  margin: 17px 0 13px;
  font-size: clamp(39px, 7vw, 70px);
  font-weight: 670;
  letter-spacing: -.065em;
  line-height: 1.02;
}
.hero-copy { max-width: 700px; margin: 0; color: var(--muted); line-height: 1.7; }
.status {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 8px 11px;
  border: 1px solid var(--border);
  border-radius: 99px;
  font-size: 12px;
  font-weight: 620;
}
.status::before { width: 8px; height: 8px; border-radius: 50%; background: currentColor; content: ""; }
.status-clean, .verdict-resolved { color: var(--green); }
.status-action_required, .verdict-accept { color: var(--cyan); }
.status-human_review_required, .verdict-human_review { color: var(--amber); }
.status-failed { color: var(--rose); }
.verdict-reject, .verdict-non_blocking { color: var(--muted); }
.metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 13px;
  margin: 35px 0 24px;
}
.metric, .panel {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  backdrop-filter: blur(14px);
}
.metric { min-height: 118px; padding: 20px 21px; }
.metric-value { margin-top: 12px; font-size: 29px; font-weight: 650; letter-spacing: -.045em; }
.metric-note { margin-top: 5px; color: var(--muted); font-size: 11px; }
.overview { display: grid; grid-template-columns: 1.3fr 1fr; gap: 15px; }
.panel { padding: 23px 25px; }
.panel h2, .section-title { margin: 0 0 16px; font-size: 14px; font-weight: 620; }
.snapshot-row {
  display: flex;
  justify-content: space-between;
  gap: 20px;
  margin: 11px 0;
  font-size: 12px;
}
.snapshot-row span:first-child { color: var(--muted); }
.snapshot-row code, code, pre {
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
}
.budget-value { font-size: 30px; font-weight: 640; letter-spacing: -.05em; }
.budget-value.over-budget { color: var(--amber); }
.budget-track { height: 8px; margin: 14px 0 8px; overflow: hidden; border-radius: 6px; background: #252939; }
.budget-fill { height: 100%; border-radius: 6px; background: linear-gradient(90deg, var(--violet), var(--cyan)); }
.budget-fill.over-budget { background: linear-gradient(90deg, var(--amber), var(--rose)); }
.budget-note { color: var(--muted); font-size: 11px; line-height: 1.6; }
.findings { margin-top: 50px; }
.section-heading { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; }
.section-title { margin: 0; font-size: 16px; }
.section-note { color: var(--muted); font-size: 12px; }
.finding-card { margin-bottom: 16px; padding: 26px; }
.finding-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 18px; }
.finding-number { color: var(--muted); font-size: 11px; }
.finding-title { margin: 8px 0 0; font-size: 21px; font-weight: 620; letter-spacing: -.035em; }
.finding-meta { display: flex; flex-wrap: wrap; gap: 9px; margin-top: 14px; }
.pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 26px;
  padding: 5px 9px;
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--muted);
  font-size: 10px;
}
.verdict { white-space: nowrap; font-size: 11px; font-weight: 640; }
.finding-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px 26px; margin-top: 25px; }
.fact { min-width: 0; }
.fact .label { margin-bottom: 8px; }
.fact p { margin: 0; color: #dce0ee; font-size: 13px; line-height: 1.67; }
.recommendation {
  margin-top: 21px;
  padding: 16px 17px;
  border-left: 2px solid rgba(170, 148, 255, .72);
  border-radius: 0 11px 11px 0;
  background: rgba(170, 148, 255, .07);
}
.recommendation .label { color: var(--violet); }
.recommendation p { margin: 7px 0 0; font-size: 13px; line-height: 1.7; }
details.evidence { margin-top: 19px; border-top: 1px solid var(--border); padding-top: 15px; }
details summary { cursor: pointer; color: var(--muted); font-size: 12px; }
details summary:hover { color: var(--ink); }
.evidence-content { margin-top: 18px; }
.evidence-step {
  position: relative;
  margin-left: 5px;
  padding: 0 0 20px 20px;
  border-left: 1px solid var(--border);
}
.evidence-step:last-child { border-left-color: transparent; }
.evidence-step::before {
  position: absolute;
  top: 3px;
  left: -4px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--violet);
  content: "";
}
.evidence-step h3 { margin: 0 0 6px; font-size: 12px; }
.evidence-step p, .anchors li { margin: 0; color: var(--muted); font-size: 12px; line-height: 1.65; }
.anchors { margin: 11px 0 0; padding-left: 16px; }
.anchors li { margin-bottom: 5px; }
.commands { margin-top: 15px; }
.commands pre {
  overflow-x: auto;
  margin: 8px 0 0;
  padding: 13px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: rgba(5, 7, 12, .6);
  color: var(--cyan);
  font-size: 10px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.empty { padding: 40px; text-align: center; color: var(--muted); }
.footer {
  display: flex;
  justify-content: space-between;
  gap: 15px;
  margin-top: 33px;
  padding-top: 18px;
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: 11px;
}
@media (max-width: 760px) {
  .shell { padding: 34px 17px 60px; }
  .metrics { grid-template-columns: 1fr 1fr; }
  .overview, .finding-grid { grid-template-columns: 1fr; }
  .hero { padding-top: 48px; }
  .finding-top { flex-direction: column; }
  .footer { flex-direction: column; }
}
@media print {
  body { color: #141822; background: white; }
  .shell { padding: 0; }
  .metric, .panel { border-color: #dce0e7; background: white; }
  .hero-copy, .label, .section-note, .budget-note { color: #586070; }
  .fact p, .recommendation p { color: #171c26; }
  .finding-card { break-inside: avoid; }
}
"""


def _text(value: object) -> str:
    return escape(str(value if value is not None else ""), quote=True)


def _plain(value: object, limit: int = 360) -> str:
    text = _SPACE.sub(" ", str(value if value is not None else "")).strip()
    if len(text) <= limit:
        return text
    shortened = text[: limit - 1].rsplit(" ", 1)[0]
    return shortened.rstrip(".,;: ") + "…"


def _number(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _tokens(value: object) -> str:
    count = _number(value)
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}m"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def _effective_verdict(entry: dict[str, Any]) -> str:
    if entry.get("resolved"):
        return "resolved"
    override = entry.get("manual_override")
    if override:
        return "accept" if override.get("action") == "include" else "reject"
    decision = entry.get("decision") or {}
    return str(decision.get("verdict", "pending"))


def _challenge(entry: dict[str, Any]) -> dict[str, Any]:
    decision = entry.get("decision") or {}
    result = decision.get("challenge") or decision.get("adversarial_challenge")
    return result if isinstance(result, dict) else {}


def _verdict_label(verdict: str) -> str:
    return {
        "accept": "FIX RECOMMENDED",
        "reject": "DISMISSED",
        "non_blocking": "SAFE TO DEFER",
        "human_review": "DECISION NEEDED",
        "resolved": "RESOLVED",
        "pending": "INVESTIGATING",
    }.get(verdict, "DECISION NEEDED")


def _status_copy(status: str, summary: dict[str, Any]) -> tuple[str, str]:
    if status == "clean":
        return "Nothing is blocking this review.", (
            "Source-grounded investigation found no remaining changes that require "
            "attention. The evidence and reasoning remain available below."
        )
    if status == "human_review_required":
        count = _number(summary.get("human_review"))
        return "A decision is waiting for you.", (
            f"{count} finding{'s' if count != 1 else ''} need human judgment "
            "before a bounded repair can proceed. Review the proposed impact and "
            "complexity, then explicitly include or dismiss them."
        )
    if status == "failed":
        return "This review stopped early.", (
            "The partial session is preserved. Inspect completed findings and "
            "the reported failure before starting another review."
        )
    count = _number(summary.get("accepted"))
    return "The useful findings are ready.", (
        f"{count} source-grounded finding{'s' if count != 1 else ''} survived "
        "adversarial review. Their smallest intent-preserving fixes are "
        "summarized below."
    )


def _recommendation(
    entry: dict[str, Any], challenge: dict[str, Any], policy: dict[str, Any]
) -> str:
    verdict = _effective_verdict(entry)
    reason = _plain((entry.get("decision") or {}).get("reason"), 320)
    smallest = _plain(challenge.get("smallest_fix"), 380)
    override = entry.get("manual_override")
    if entry.get("resolved"):
        return "Already addressed by the bounded repair; the final review no longer reports it."
    if override and override.get("reason"):
        explanation = _plain(override["reason"], 260)
        if override.get("action") == "include" and smallest:
            return f"{smallest} Manually included: {explanation}"
        return explanation
    if verdict == "accept":
        return smallest or "Apply the smallest verified change that preserves the pull request’s intent."
    if verdict == "human_review":
        estimate = _number(challenge.get("estimated_added_production_lines"))
        maximum = _number(policy.get("max_added_production_lines"))
        if estimate and maximum and estimate > maximum:
            return (
                f"The proposed fix needs about {estimate} production lines, above the "
                f"{maximum}-line budget. Find a smaller fix, explicitly include it "
                "with a larger budget, or defer it."
            )
        return reason or "Inspect the evidence and explicitly include or dismiss this finding."
    if verdict == "non_blocking":
        if "argument" in str(entry.get("finding", {}).get("title", "")).lower():
            return (
                "No runtime defect is established. Apply the exact parameter comment "
                "if repository instructions require it; otherwise defer this readability issue."
            )
        return reason or "This concern does not justify blocking the current review."
    if verdict == "reject":
        return reason or "The concern was source-refuted or explicitly dismissed."
    return "Wait for the independent investigation before choosing an action."


def _evidence_step(title: str, content: object) -> str:
    body = _plain(content, 1_500)
    if not body:
        return ""
    return (
        '<div class="evidence-step">'
        f"<h3>{_text(title)}</h3><p>{_text(body)}</p></div>"
    )


def _source_anchors(entry: dict[str, Any], repo_root: Path) -> str:
    decision = entry.get("decision") or {}
    investigation = entry.get("investigations") or {}
    phase = investigation.get("final") or investigation.get("initial") or {}
    sources: list[dict[str, Any]] = []
    for candidate in (
        _challenge(entry),
        decision.get("adversarial_challenge"),
        decision.get("reviewer_response"),
        phase.get("observation"),
        phase.get("adversary"),
        phase.get("reviewer_response"),
    ):
        if isinstance(candidate, dict):
            sources.extend(
                anchor
                for anchor in candidate.get("source_anchors", [])
                if isinstance(anchor, dict)
            )
    unique: dict[tuple[str, int], dict[str, Any]] = {}
    for anchor in sources:
        path = str(anchor.get("path", ""))
        line = _number(anchor.get("line"))
        if path and line > 0:
            unique.setdefault((path, line), anchor)
    if not unique:
        return ""
    items: list[str] = []
    for (relative, line), anchor in list(unique.items())[:14]:
        candidate = (repo_root / relative).resolve()
        label = f"{relative}:{line}"
        if candidate.is_relative_to(repo_root):
            href = candidate.as_uri() + f"#L{line}"
            location = f'<a href="{_text(href)}">{_text(label)}</a>'
        else:
            location = f"<code>{_text(label)}</code>"
        explanation = _plain(anchor.get("explanation"), 180)
        items.append(f"<li>{location} — {_text(explanation)}</li>")
    return '<ul class="anchors">' + "".join(items) + "</ul>"


def _commands(entry: dict[str, Any], session: dict[str, Any]) -> str:
    finding = entry.get("finding") or {}
    selector = str(finding.get("finding_id", ""))[:10]
    repo = str((session.get("snapshot") or {}).get("repo_root", ""))
    session_id = str(session.get("session_id", "latest"))
    verdict = _effective_verdict(entry)
    actions = ("dismiss",) if verdict == "accept" else ("include", "dismiss")
    commands = []
    for action in actions:
        arguments = [
            "review-reducer", "session", action, session_id, selector,
            "--repo", repo, "--reason", "Reviewed the saved evidence",
        ]
        commands.append(shlex.join(arguments))
    return '<div class="commands"><div class="label">Review controls</div><pre>' + _text(
        "\n".join(commands)
    ) + "</pre></div>"


def _finding_card(
    entry: dict[str, Any], index: int, session: dict[str, Any], policy: dict[str, Any]
) -> str:
    finding = entry.get("finding") or {}
    decision = entry.get("decision") or {}
    challenge = _challenge(entry)
    investigation = entry.get("investigations") or {}
    phase = investigation.get("final") or investigation.get("initial") or {}
    observation = phase.get("observation") or decision.get("observation") or {}
    adversary = phase.get("adversary") or decision.get("adversarial_challenge") or {}
    rebuttal = phase.get("reviewer_response") or decision.get("reviewer_response") or {}
    verdict = _effective_verdict(entry)
    finding_id = _SAFE_ANCHOR.sub("", str(finding.get("finding_id", index)))
    title = _PRIORITY.sub("", str(finding.get("title", "Untitled finding")))
    explanation = _plain(
        challenge.get("root_cause") or finding.get("body") or observation.get("changed_behavior")
    )
    impact = _plain(challenge.get("user_impact") or observation.get("user_impact"))
    trigger = _plain(challenge.get("realistic_trigger") or observation.get("realistic_trigger"))
    rationale = _plain(decision.get("reason") or challenge.get("rationale"))
    confidence = challenge.get("confidence")
    confidence_label = (
        f"{confidence:.0%} confidence"
        if isinstance(confidence, (float, int)) and not isinstance(confidence, bool)
        else "Evidence preserved"
    )
    estimate = _number(challenge.get("estimated_added_production_lines"))
    estimate_label = f"≈ {estimate} production lines" if estimate else "No added lines estimated"
    location = str(finding.get("path", ""))
    if finding.get("line_start"):
        location += f":{_number(finding['line_start'])}"
    badges = "".join(
        f'<span class="pill">{_text(value)}</span>'
        for value in (
            f"P{_number(finding.get('priority'))}",
            str(challenge.get("impact", "unknown")).capitalize() + " impact",
            confidence_label,
            estimate_label,
            location,
        )
    )
    facts = (
        ("What is happening", explanation or "No explanation was recorded."),
        ("Why it matters", impact or "No user-facing impact was established."),
        ("Realistic trigger", trigger or "A concrete trigger was not established."),
        ("Why this decision", rationale or "Review reasoning is available below."),
    )
    fact_html = "".join(
        f'<div class="fact"><div class="label">{_text(label)}</div>'
        f"<p>{_text(content)}</p></div>"
        for label, content in facts
    )
    steps = "".join(
        (
            _evidence_step("Original reviewer", finding.get("body")),
            _evidence_step("Blind investigation", observation.get("changed_behavior")),
            _evidence_step("Adversarial assessment", adversary.get("rationale")),
            _evidence_step("Reviewer rebuttal", rebuttal.get("rationale")),
            _evidence_step("Final decision", decision.get("reason")),
            _evidence_step(
                "Manual override", (entry.get("manual_override") or {}).get("reason")
            ),
        )
    )
    repo_root = Path(str((session.get("snapshot") or {}).get("repo_root", "."))).resolve()
    sources = _source_anchors(entry, repo_root)
    recommendation = _recommendation(entry, challenge, policy)
    return (
        f'<article class="panel finding-card" id="finding-{_text(finding_id)}">'
        '<div class="finding-top"><div>'
        f'<div class="finding-number">FINDING {index:02d}</div>'
        f'<h2 class="finding-title">{_text(title)}</h2>'
        f'<div class="finding-meta">{badges}</div></div>'
        f'<span class="verdict verdict-{_text(verdict)}">'
        f"{_text(_verdict_label(verdict))}</span></div>"
        f'<div class="finding-grid">{fact_html}</div>'
        '<div class="recommendation"><div class="label">Recommended action</div>'
        f"<p>{_text(recommendation)}</p></div>"
        '<details class="evidence"><summary>Inspect the full review evidence</summary>'
        f'<div class="evidence-content">{steps}{sources}{_commands(entry, session)}</div>'
        "</details></article>"
    )


def render_html_report(report: dict[str, Any], session: dict[str, Any]) -> str:
    """Render a complete offline document without extra model turns or scripts."""

    snapshot = session.get("snapshot") or report.get("snapshot") or {}
    summary = session.get("summary") or {}
    usage = report.get("usage") or session.get("usage") or {}
    configured_policy = report.get("policy") or session.get("policy") or {}
    policy = {
        "max_added_production_lines": 20,
        "max_additional_production_files": 2,
    }
    if isinstance(configured_policy, dict):
        policy.update(configured_policy)
    status = str(session.get("status") or report.get("status") or "clean")
    headline, description = _status_copy(status, summary)
    entries = list(session.get("findings") or [])
    accepted = [entry for entry in entries if _effective_verdict(entry) == "accept"]
    estimated_lines = sum(
        _number(_challenge(entry).get("estimated_added_production_lines"))
        for entry in accepted
    )
    estimated_files = len({
        str((entry.get("finding") or {}).get("path", ""))
        for entry in accepted
        if (entry.get("finding") or {}).get("path")
    })
    max_lines = _number(policy.get("max_added_production_lines")) or 20
    max_files = _number(policy.get("max_additional_production_files")) or 2
    over_budget = estimated_lines > max_lines or estimated_files > max_files
    budget_ratio = min(100, round(estimated_lines / max(max_lines, 1) * 100))
    budget_class = " over-budget" if over_budget else ""
    metric_values = (
        ("Ready to fix", _number(summary.get("accepted")), "Source-grounded findings"),
        ("Need judgment", _number(summary.get("human_review")), "Awaiting your decision"),
        (
            "Dismissed / deferred",
            _number(summary.get("rejected")) + _number(summary.get("non_blocking")),
            "Kept out of the fix batch",
        ),
        ("Codex turns", _number(usage.get("turn_count")), f"{_tokens(usage.get('cached_input_tokens'))} cached input"),
    )
    metrics = "".join(
        '<div class="metric">'
        f'<div class="label">{_text(label)}</div>'
        f'<div class="metric-value">{_text(value)}</div>'
        f'<div class="metric-note">{_text(note)}</div></div>'
        for label, value, note in metric_values
    )
    details = (
        ("Repository", Path(str(snapshot.get("repo_root", "repository"))).name),
        ("Comparison", str(snapshot.get("base_ref", "unknown"))),
        ("Commit", str(snapshot.get("head_sha", ""))[:12]),
        ("Session", str(session.get("session_id", "unknown"))[:25]),
        ("Model input", _tokens(usage.get("input_tokens"))),
        ("Model output", _tokens(usage.get("output_tokens"))),
    )
    snapshot_rows = "".join(
        f'<div class="snapshot-row"><span>{_text(label)}</span>'
        f"<code>{_text(value)}</code></div>"
        for label, value in details
    )
    ordered = sorted(
        enumerate(entries, start=1),
        key=lambda item: {
            "human_review": 0,
            "accept": 1,
            "non_blocking": 2,
            "reject": 3,
            "resolved": 4,
        }.get(_effective_verdict(item[1]), 5),
    )
    findings = "".join(
        _finding_card(entry, index, session, policy) for index, entry in ordered
    ) or '<div class="panel empty">No review findings were reported.</div>'
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    title = f"Review reducer · {Path(str(snapshot.get('repo_root', 'repository'))).name}"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
  <meta name="color-scheme" content="dark">
  <title>{_text(title)}</title>
  <style>{_CSS}</style>
</head>
<body>
<main class="shell">
  <header class="masthead">
    <div class="brand"><span class="brand-mark">◈</span> CODEX REVIEW REDUCER</div>
    <div class="masthead-meta">LOCAL · SELF-CONTAINED<br>{_text(generated)}</div>
  </header>
  <section class="hero">
    <span class="status status-{_text(status)}">{_text(status.replace('_', ' ').upper())}</span>
    <h1>{_text(headline)}</h1>
    <p class="hero-copy">{_text(description)}</p>
    <div class="metrics">{metrics}</div>
    <div class="overview">
      <section class="panel"><h2>Pinned review context</h2>{snapshot_rows}</section>
      <section class="panel"><h2>Estimated repair complexity</h2>
        <div class="budget-value{budget_class}">{estimated_lines} / {max_lines} lines</div>
        <div class="budget-track"><div class="budget-fill{budget_class}" style="width: {budget_ratio}%"></div></div>
        <div class="budget-note">{estimated_files} / {max_files} production files · estimates apply to currently included findings</div>
      </section>
    </div>
  </section>
  <section class="findings"><div class="section-heading"><h2 class="section-title">Review findings</h2>
    <span class="section-note">{len(entries)} inspected · evidence preserved</span></div>{findings}</section>
  <footer class="footer"><span>Generated locally from pinned review evidence. No external resources or additional model calls.</span>
    <span>{_text(str(session.get('session_id', '')))}</span></footer>
</main>
</body>
</html>
"""


def write_html_report(
    session: ReviewSession,
    *,
    report: dict[str, Any] | None = None,
    output: Path | None = None,
) -> Path:
    """Persist a private standalone report without dirtying the reviewed tree."""

    if report is None:
        try:
            loaded = json.loads((session.run_dir / "report.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ReviewReducerError(
                f"cannot load completed review data for the HTML report: {error}"
            ) from error
        if not isinstance(loaded, dict):
            raise ReviewReducerError("the completed review report is not a JSON object")
        report = loaded
    destination = (output or session.run_dir / "report.html").expanduser().resolve()
    repo_root = Path(str((session.data.get("snapshot") or {}).get("repo_root", "."))).resolve()
    common_dir = git_common_dir(repo_root)
    if destination.is_relative_to(repo_root) and not destination.is_relative_to(common_dir):
        raise ReviewReducerError(
            "the HTML report cannot live inside the reviewed working tree; "
            "use its Git metadata directory or an external path"
        )
    document = render_html_report(report, session.data)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(document, encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(destination)
    except OSError as error:
        raise ReviewReducerError(f"cannot write the HTML report: {error}") from error
    if output is None and session.data.get("html_report") != str(destination):
        session.data["html_report"] = str(destination)
        session.save()
    return destination
